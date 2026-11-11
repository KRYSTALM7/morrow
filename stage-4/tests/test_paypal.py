import json
import io
import os
from pathlib import Path
import urllib.error
import uuid

import pytest

import fixtures as fx
from app import ApiError, Service
from payments_store import CheckoutStore
from paypal import PayPalClient, PayPalError, SANDBOX_BASE_URL


class Response:
    def __init__(self, payload, status=200):
        self.payload, self.status = payload, status

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return False

    def read(self):
        return json.dumps(self.payload).encode()


class Transport:
    def __init__(self, responses):
        self.responses = list(responses)
        self.requests = []

    def __call__(self, request, timeout=0):
        self.requests.append(request)
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return Response(*response) if isinstance(response, tuple) else Response(response)


class SandboxTransport:
    def __init__(self):
        self.create_requests = []
        self.capture_requests = []
        self.order_reads = []
        self.webhook_valid = True
        self.capture_errors = []
        self.create_errors = []
        self.order_counter = 0
        self.orders = {}
        self.verification_requests = []

    def __call__(self, request, timeout=0):
        path = request.full_url.removeprefix(SANDBOX_BASE_URL)
        method = request.get_method()
        if path == "/v1/oauth2/token":
            return Response({"access_token": "sandbox-access-token", "expires_in": 3600})
        if path == "/v2/checkout/orders" and method == "POST":
            payload = json.loads(request.data)
            self.create_requests.append((request, payload))
            if self.create_errors:
                error = self.create_errors.pop(0)
                if error:
                    raise TimeoutError("mocked order creation timeout")
            self.order_counter += 1
            order_id = "ORDER-123" if self.order_counter == 1 else f"ORDER-{self.order_counter}"
            order = {"id": order_id, "status": "CREATED", "purchase_units": payload["purchase_units"],
                     "links": [{"rel": "approve", "href": f"https://www.sandbox.paypal.com/checkoutnow?token={order_id}"}]}
            self.orders[order_id] = order
            return Response(order)
        if path.startswith("/v2/checkout/orders/") and method == "GET":
            order_id = path.rsplit("/", 1)[-1]
            self.order_reads.append(order_id)
            return Response(self.orders[order_id])
        if path.startswith("/v2/checkout/orders/") and path.endswith("/capture"):
            order_id = path.split("/")[-2]
            self.capture_requests.append((request, order_id))
            if self.capture_errors:
                error = self.capture_errors.pop(0)
                if error.retryable:
                    raise TimeoutError("mocked provider timeout")
                raw = json.dumps({"name": error.code, "message": str(error)}).encode()
                raise urllib.error.HTTPError(request.full_url, error.status or 422,
                                             "mocked capture failure", {}, io.BytesIO(raw))
            return Response(self.complete_order(order_id))
        if path == "/v1/notifications/verify-webhook-signature":
            self.verification_requests.append(json.loads(request.data))
            return Response({"verification_status": "SUCCESS" if self.webhook_valid else "FAILURE"})
        raise AssertionError(f"unexpected mocked PayPal request {method} {path}")

    def complete_order(self, order_id):
        order = self.orders[order_id]
        order["status"] = "COMPLETED"
        for unit in order["purchase_units"]:
            unit["payments"] = {"captures": [{"id": f"CAPTURE-{order_id}", "status": "COMPLETED",
                                               "amount": unit["amount"]}]}
        return order

    def force_order_status(self, order_id, status):
        if status == "COMPLETED":
            self.complete_order(order_id)
        else:
            self.orders[order_id]["status"] = status


@pytest.fixture
def svc(monkeypatch):
    db_path = Path(__file__).resolve().parents[1] / f".paypal-test-{uuid.uuid4().hex}.sqlite3"
    wallet_db_path = Path(str(db_path).replace(".sqlite3", "-wallet.sqlite3"))
    monkeypatch.setenv("PAYPAL_DB_PATH", str(db_path))
    monkeypatch.setenv("MORROW_WALLET_DB_PATH", str(wallet_db_path))
    monkeypatch.setenv("PAYPAL_CLIENT_ID", "sandbox-client")
    monkeypatch.setenv("PAYPAL_CLIENT_SECRET", "sandbox-secret")
    monkeypatch.setenv("PAYPAL_BASE_URL", SANDBOX_BASE_URL)
    service = Service()
    service.state = service._reset(fx.fixture())
    with service.wallet_store.transaction():
        service.wallet_store.create_session("ada-token", "u_ada",
                                             expires_at="2999-01-01T00:00:00+00:00")
        service.wallet_store.create_session("bob-token", "u_bob",
                                             expires_at="2999-01-01T00:00:00+00:00")
        service.wallet_store.save(service.state)
    service.paypal_transport = SandboxTransport()
    service.paypal = PayPalClient(client_id="sandbox-client", client_secret="sandbox-secret",
                                  base_url=SANDBOX_BASE_URL, webhook_id="WH-TEST",
                                  transport=service.paypal_transport)
    yield service
    for base in (db_path, wallet_db_path):
        for suffix in ("", "-shm", "-wal"):
            try:
                os.remove(str(base) + suffix)
            except FileNotFoundError:
                pass


def call(service, method, path, body=None, *, token="ada-token", key="key-1", extra_headers=None):
    headers = dict(extra_headers or {})
    if token is not None:
        headers["Authorization"] = f"Bearer {token}"
    if key is not None:
        headers["Idempotency-Key"] = key
    raw = json.dumps(body or {}).encode()
    return service.dispatch(method, path, headers, raw)


def make_checkout(service, *, key="key-1", token="ada-token", amount=1234):
    return call(service, "POST", "/paypal/checkouts",
                {"amount_minor": amount, "note": "Shared dinner with Bob"}, token=token, key=key)


def webhook_headers():
    return {"paypal-transmission-id": "tx", "paypal-transmission-time": "2026-11-03T00:00:00Z",
            "paypal-cert-url": "https://api-m.sandbox.paypal.com/cert", "paypal-auth-algo": "SHA256withRSA",
            "paypal-transmission-sig": "signature"}


def test_oauth_token_order_create_approval_and_request_id():
    transport = Transport([
        {"access_token": "oauth-token", "expires_in": 3600},
        {"id": "ORDER-7", "status": "CREATED", "links": [
            {"rel": "approve", "href": "https://www.sandbox.paypal.com/checkoutnow?token=ORDER-7"}]},
    ])
    client = PayPalClient(client_id="id", client_secret="secret", transport=transport)
    order = client.create_order(checkout_id="checkout-7", amount_minor=1234, currency="EUR",
                                minor_units=2, note="dinner", return_url="http://localhost:8080/paypal/return",
                                cancel_url="http://localhost:8080/paypal/return?cancelled=1",
                                request_id="stable-request-id")
    assert order["approval_url"].startswith("https://www.sandbox.paypal.com/")
    token_request, order_request = transport.requests
    assert token_request.full_url.endswith("/v1/oauth2/token")
    assert token_request.get_header("Authorization").startswith("Basic ")
    assert next(value for key, value in order_request.headers.items()
                if key.lower() == "paypal-request-id") == "stable-request-id"
    payload = json.loads(order_request.data)
    assert payload["intent"] == "CAPTURE"
    assert payload["purchase_units"][0]["amount"] == {"currency_code": "EUR", "value": "12.34"}


def test_server_order_capture_replay_and_history(svc):
    status, created = make_checkout(svc)
    assert status == 201
    assert created["status"] == "CREATED"
    assert created["order_id"] == "ORDER-123"
    assert created["approval_url"].startswith("https://www.sandbox.paypal.com/")
    assert svc.state["users"]["u_ada"]["balance"] == fx.ADA["balance"]
    assert svc.state["payments"] == []

    replay_status, replay = make_checkout(svc)
    assert replay_status == 200
    assert replay["checkout_id"] == created["checkout_id"]
    assert len(svc.paypal_transport.create_requests) == 1

    svc.paypal_transport.force_order_status("ORDER-123", "APPROVED")
    captured_status, captured = call(svc, "POST", f"/paypal/checkouts/{created['checkout_id']}/capture", {})
    assert captured_status == 200
    assert captured["status"] == "COMPLETED"
    assert captured["capture_id"] == "CAPTURE-ORDER-123"
    _, duplicate_capture = call(svc, "POST", f"/paypal/checkouts/{created['checkout_id']}/capture", {})
    assert duplicate_capture["status"] == "COMPLETED"
    assert len(svc.paypal_transport.capture_requests) == 1

    _, history = call(svc, "GET", "/paypal/checkouts")
    assert history["checkouts"][0]["capture_id"] == "CAPTURE-ORDER-123"
    reopened = CheckoutStore(svc.paypal_store.path)
    assert reopened.get(created["checkout_id"])["status"] == "COMPLETED"
    assert len(reopened.audit(created["checkout_id"])) >= 4


def test_checkout_idempotency_conflict_and_user_isolation(svc):
    _, created = make_checkout(svc)
    with pytest.raises(ApiError, match="another checkout") as conflict:
        make_checkout(svc, amount=999)
    assert conflict.value.status == 409
    with pytest.raises(ApiError) as forbidden:
        call(svc, "GET", f"/paypal/checkouts/{created['checkout_id']}", token="bob-token", key=None)
    assert forbidden.value.status == 404
    with pytest.raises(ApiError) as unauthenticated:
        call(svc, "POST", "/paypal/checkouts", {"amount_minor": 100}, token=None)
    assert unauthenticated.value.status == 401


def test_ai_paypal_proposal_requires_confirmation_then_redirects_to_provider(svc):
    _, proposal = call(svc, "POST", "/ai/proposals",
                       {"message": "Prepare €12.34 payment for Bob"})
    assert proposal["action"] == "paypal_checkout"
    assert proposal["context_handle"] == "bob"
    assert svc.paypal_transport.create_requests == []

    confirm_path = f"/ai/proposals/{proposal['proposal_id']}/confirm"
    status, confirmed = call(svc, "POST", confirm_path, {"confirm": True}, key="confirm-checkout")
    assert status == 201
    assert confirmed["confirmed"] is True
    assert confirmed["result"]["approval_url"].startswith("https://www.sandbox.paypal.com/")
    assert confirmed["result"]["merchant_label"] == "Morrow Sandbox merchant"
    assert svc.state["payments"] == []
    assert svc.state["users"]["u_ada"]["balance"] == fx.ADA["balance"]
    replay_status, replay = call(svc, "POST", confirm_path, {"confirm": True}, key="confirm-checkout")
    assert replay_status == 200
    assert replay == confirmed
    assert len(svc.paypal_transport.create_requests) == 1


def test_order_create_unknown_retries_same_paypal_request_id(svc):
    svc.paypal_transport.create_errors = [True]
    with pytest.raises(ApiError) as unknown:
        make_checkout(svc, key="create-retry")
    assert unknown.value.code == "paypal_network_error"
    existing = svc.paypal_store.get_for_key("u_ada", "create-retry")
    assert existing["status"] == "CREATE_UNKNOWN"
    status, checkout = make_checkout(svc, key="create-retry")
    assert status == 201
    assert checkout["status"] == "CREATED"
    requests = [request for request, _ in svc.paypal_transport.create_requests]
    request_ids = [next(value for header, value in request.headers.items()
                         if header.lower() == "paypal-request-id") for request in requests]
    assert request_ids[0] == request_ids[1]


def test_capture_requires_authoritative_paypal_approval(svc):
    _, created = make_checkout(svc)
    svc.paypal_transport.force_order_status("ORDER-123", "CREATED")
    with pytest.raises(ApiError) as failure:
        call(svc, "POST", f"/paypal/checkouts/{created['checkout_id']}/capture", {})
    assert failure.value.status == 409
    assert failure.value.code == "buyer_approval_required"
    assert svc.paypal_transport.capture_requests == []


def test_capture_failure_and_unknown_capture_are_recoverable(svc):
    _, created = make_checkout(svc)
    svc.paypal_transport.force_order_status("ORDER-123", "APPROVED")
    svc.paypal_transport.capture_errors = [PayPalError("declined", status=422, code="INSTRUMENT_DECLINED")]
    with pytest.raises(ApiError):
        call(svc, "POST", f"/paypal/checkouts/{created['checkout_id']}/capture", {})
    assert svc.paypal_store.get(created["checkout_id"])["status"] == "FAILED"
    assert len(svc.paypal_transport.capture_requests) == 1

    _, retryable = make_checkout(svc, key="unknown-capture")
    svc.paypal_transport.force_order_status("ORDER-2", "APPROVED")
    svc.paypal_transport.capture_errors = [PayPalError("timeout", code="timeout", retryable=True)]
    with pytest.raises(ApiError):
        call(svc, "POST", f"/paypal/checkouts/{retryable['checkout_id']}/capture", {})
    assert svc.paypal_store.get(retryable["checkout_id"])["status"] == "CAPTURE_UNKNOWN"
    _, recovered = call(svc, "POST", f"/paypal/checkouts/{retryable['checkout_id']}/capture", {})
    assert recovered["status"] == "COMPLETED"
    requests = [request for request, _ in svc.paypal_transport.capture_requests]
    request_ids = [next(value for header, value in request.headers.items()
                         if header.lower() == "paypal-request-id") for request in requests]
    assert request_ids[1] == request_ids[2]


def test_invalid_webhook_rejected_and_valid_duplicate_deduplicated(svc):
    _, created = make_checkout(svc)
    event = {"id": "WH-EVENT-1", "event_type": "PAYMENT.CAPTURE.COMPLETED",
             "resource": {"id": "CAPTURE-ORDER-123", "supplementary_data": {"related_ids": {"order_id": "ORDER-123"}}}}
    headers = webhook_headers()
    svc.paypal_transport.webhook_valid = False
    with pytest.raises(ApiError) as invalid:
        svc.dispatch("POST", "/webhooks/paypal", headers, json.dumps(event).encode())
    assert invalid.value.code == "invalid_webhook_signature"
    assert svc.paypal_store.get_for_order("ORDER-123")["status"] == "CREATED"

    svc.paypal_transport.webhook_valid = True
    svc.paypal_transport.force_order_status("ORDER-123", "COMPLETED")
    _, first = svc.dispatch("POST", "/webhooks/paypal", headers, json.dumps(event).encode())
    assert first["matched"] is True
    count = len(svc.paypal_transport.order_reads)
    _, duplicate = svc.dispatch("POST", "/webhooks/paypal", headers, json.dumps(event).encode())
    assert duplicate["duplicate"] is True
    assert len(svc.paypal_transport.order_reads) == count
    verification = svc.paypal_transport.verification_requests[-1]
    assert verification["webhook_id"] == "WH-TEST"
    assert verification["webhook_event"] == event
    assert svc.paypal_store.get(created["checkout_id"])["status"] == "COMPLETED"


def test_webhook_out_of_order_event_does_not_regress_provider_state(svc):
    _, created = make_checkout(svc)
    svc.paypal_transport.force_order_status("ORDER-123", "COMPLETED")
    for event_id, event_type in (("evt-complete", "PAYMENT.CAPTURE.COMPLETED"),
                                  ("evt-pending", "PAYMENT.CAPTURE.PENDING")):
        event = {"id": event_id, "event_type": event_type,
                 "resource": {"id": "CAPTURE-ORDER-123", "supplementary_data": {"related_ids": {"order_id": "ORDER-123"}}}}
        svc.dispatch("POST", "/webhooks/paypal", webhook_headers(),
                     json.dumps(event).encode())
    assert svc.paypal_store.get(created["checkout_id"])["status"] == "COMPLETED"
    assert [row["event"] for row in svc.paypal_store.audit(created["checkout_id"])][-1] == "verified_webhook_reconciled"


def test_order_capture_failure_and_sandbox_url_validation():
    with pytest.raises(ValueError, match="Sandbox only"):
        PayPalClient(client_id="id", client_secret="secret", base_url="https://api-m.paypal.com")
    with pytest.raises(ValueError, match="Sandbox only"):
        PayPalClient(client_id="id", client_secret="secret", base_url="https://api-m.sandbox.paypal.com.attacker.test")
    client = PayPalClient(client_id="id", client_secret="secret", webhook_id="WH-1",
                          transport=Transport([]))
    assert client.verify_webhook({**webhook_headers(),
                                  "paypal-cert-url": "https://paypal.com.attacker.test/cert"}, {}) is False
    assert client.verify_webhook({**webhook_headers(),
                                  "paypal-cert-url": "https://evilpaypal.com/cert"}, {}) is False
    transport = Transport([urllib.error.HTTPError("https://sandbox", 401, "Unauthorized", {}, None)])
    client = PayPalClient(client_id="id", client_secret="secret", transport=transport)
    with pytest.raises(PayPalError):
        client.access_token()


def test_test_only_reset_export_import_routes_are_disabled_by_default(svc, monkeypatch):
    monkeypatch.delenv("POCKETFUL_ENABLE_TEST_ROUTES", raising=False)
    with pytest.raises(ApiError) as hidden:
        svc.dispatch("GET", "/_test/export", {}, b"")
    assert hidden.value.status == 404
