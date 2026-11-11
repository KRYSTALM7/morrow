"""PayPal Orders v2 Sandbox adapter, implemented with the Python standard library."""
from __future__ import annotations

import base64
import json
import os
import threading
import time
import urllib.error
import urllib.parse
import urllib.request


SANDBOX_BASE_URL = "https://api-m.sandbox.paypal.com"


class PayPalError(Exception):
    def __init__(self, message: str, *, status: int | None = None, code: str = "paypal_error",
                 retryable: bool = False):
        super().__init__(message)
        self.status, self.code, self.retryable = status, code, retryable


class PayPalClient:
    def __init__(self, *, client_id: str | None = None, client_secret: str | None = None,
                 base_url: str | None = None, webhook_id: str | None = None,
                 transport=None, timeout: float = 15):
        self.client_id = client_id if client_id is not None else os.getenv("PAYPAL_CLIENT_ID", "")
        self.client_secret = client_secret if client_secret is not None else os.getenv("PAYPAL_CLIENT_SECRET", "")
        self.base_url = (base_url or os.getenv("PAYPAL_BASE_URL") or SANDBOX_BASE_URL).rstrip("/")
        self.webhook_id = webhook_id if webhook_id is not None else os.getenv("PAYPAL_WEBHOOK_ID", "")
        host = urllib.parse.urlsplit(self.base_url).hostname or ""
        if (urllib.parse.urlsplit(self.base_url).scheme != "https"
                or not (host == "sandbox.paypal.com" or host.endswith(".sandbox.paypal.com"))):
            raise ValueError("PayPal Sandbox only: PAYPAL_BASE_URL must be an HTTPS Sandbox host")
        self.transport = transport or urllib.request.urlopen
        self.timeout = timeout
        self._token = None
        self._token_expiry = 0.0
        self._token_lock = threading.Lock()

    @property
    def configured(self):
        return bool(self.client_id and self.client_secret)

    def _raw(self, url, *, method="GET", headers=None, data=None):
        request = urllib.request.Request(url, data=data, headers=headers or {}, method=method)
        try:
            with self.transport(request, timeout=self.timeout) as response:
                raw = response.read()
                status = getattr(response, "status", 200)
        except urllib.error.HTTPError as exc:
            raw = exc.read()
            try:
                payload = json.loads(raw.decode("utf-8"))
            except (ValueError, UnicodeDecodeError):
                payload = {}
            issue = payload.get("name") or payload.get("message")
            details = payload.get("details")
            if isinstance(details, list) and details and isinstance(details[0], dict):
                issue = details[0].get("issue") or issue
            message = issue or payload.get("message") or "PayPal Sandbox request failed"
            raise PayPalError(str(message), status=exc.code,
                              code=str(issue or "paypal_error"), retryable=exc.code >= 500 or exc.code == 429) from None
        except (OSError, TimeoutError) as exc:
            raise PayPalError("PayPal Sandbox could not be reached", code="paypal_network_error", retryable=True) from exc
        try:
            payload = json.loads(raw.decode("utf-8")) if raw else {}
        except (ValueError, UnicodeDecodeError) as exc:
            raise PayPalError("PayPal returned an unreadable response", code="paypal_bad_response", retryable=True) from exc
        if not 200 <= status < 300:
            raise PayPalError("PayPal Sandbox request failed", status=status,
                              retryable=status >= 500 or status == 429)
        return payload

    def access_token(self):
        if not self.configured:
            raise PayPalError("Sandbox credentials are not configured", code="paypal_not_configured")
        with self._token_lock:
            if self._token and time.time() < self._token_expiry - 30:
                return self._token
            auth = base64.b64encode(f"{self.client_id}:{self.client_secret}".encode()).decode()
            payload = self._raw(self.base_url + "/v1/oauth2/token", method="POST",
                                headers={"Authorization": "Basic " + auth,
                                         "Content-Type": "application/x-www-form-urlencoded",
                                         "Accept": "application/json"}, data=b"grant_type=client_credentials")
            token = payload.get("access_token")
            if not isinstance(token, str) or not token:
                raise PayPalError("PayPal did not return an access token", code="paypal_bad_response", retryable=True)
            self._token = token
            self._token_expiry = time.time() + int(payload.get("expires_in", 300))
            return token

    def _api(self, method, path, payload=None, request_id=None):
        headers = {"Authorization": "Bearer " + self.access_token(), "Accept": "application/json"}
        data = None
        if payload is not None:
            headers["Content-Type"] = "application/json"
            data = json.dumps(payload, separators=(",", ":")).encode()
        if request_id:
            headers["PayPal-Request-Id"] = request_id
        return self._raw(self.base_url + path, method=method, headers=headers, data=data)

    def create_order(self, *, checkout_id, amount_minor, currency, minor_units, note,
                     return_url, cancel_url, request_id):
        if currency not in {"EUR", "JPY"} or (currency == "JPY" and minor_units != 0) or (
                currency == "EUR" and minor_units != 2):
            raise PayPalError("This Morrow currency is not enabled for PayPal Sandbox checkout",
                              code="unsupported_currency")
        scale = 10 ** minor_units
        value = str(amount_minor // scale)
        if minor_units:
            value += "." + str(amount_minor % scale).zfill(minor_units)
        payload = {"intent": "CAPTURE", "purchase_units": [{
            "reference_id": checkout_id,
            "custom_id": checkout_id,
            "description": (note or "Morrow Sandbox merchant checkout")[:127],
            "amount": {"currency_code": currency, "value": value}}],
            "application_context": {"return_url": return_url, "cancel_url": cancel_url,
                                     "user_action": "PAY_NOW", "shipping_preference": "NO_SHIPPING"}}
        result = self._api("POST", "/v2/checkout/orders", payload, request_id)
        order_id = result.get("id")
        approval = next((link.get("href") for link in result.get("links", [])
                         if link.get("rel") in ("approve", "payer-action")
                         and isinstance(link.get("href"), str)), None)
        if not isinstance(order_id, str) or not order_id or not approval:
            raise PayPalError("PayPal order is missing its ID or approval link", code="paypal_bad_response", retryable=True)
        host = urllib.parse.urlsplit(approval).hostname or ""
        if urllib.parse.urlsplit(approval).scheme != "https" or not host.endswith(".sandbox.paypal.com"):
            raise PayPalError("PayPal returned an invalid Sandbox approval link", code="paypal_bad_response")
        return {"id": order_id, "status": result.get("status", "CREATED"), "approval_url": approval}

    def get_order(self, order_id):
        return self._api("GET", "/v2/checkout/orders/" + urllib.parse.quote(order_id, safe=""))

    def capture_order(self, order_id, request_id):
        return self._api("POST", "/v2/checkout/orders/" + urllib.parse.quote(order_id, safe="") + "/capture",
                         {}, request_id)

    def verify_webhook(self, headers, event):
        if not self.webhook_id:
            raise PayPalError("PAYPAL_WEBHOOK_ID is not configured", code="webhook_not_configured")
        names = {key.lower(): value for key, value in headers.items()}
        required = {"transmission_id": "paypal-transmission-id",
                    "transmission_time": "paypal-transmission-time",
                    "cert_url": "paypal-cert-url", "auth_algo": "paypal-auth-algo",
                    "transmission_sig": "paypal-transmission-sig"}
        values = {key: names.get(header) for key, header in required.items()}
        if any(not isinstance(value, str) or not value for value in values.values()):
            return False
        cert = urllib.parse.urlsplit(values["cert_url"])
        cert_host = (cert.hostname or "").lower().rstrip(".")
        if cert.scheme != "https" or not cert_host or not (
                cert_host == "paypal.com" or cert_host.endswith(".paypal.com")):
            return False
        result = self._api("POST", "/v1/notifications/verify-webhook-signature", {
            **values, "webhook_id": self.webhook_id, "webhook_event": event})
        return result.get("verification_status") == "SUCCESS"
