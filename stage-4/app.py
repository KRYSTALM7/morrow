"""Morrow Stage 4 HTTP service. Standard-library only."""
from __future__ import annotations

import copy
import calendar
import hashlib
import hmac
import json
import math
import mimetypes
import os
import re
import secrets
import threading
import tempfile
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from ai_agent import DeterministicProvider, IntentError
from finance_store import (FinanceConflict, FinanceNotFound, FinanceStore,
                           FinanceValidation, IdempotencyConflict)
from forecast_engine import (MODEL_VERSION, calculate_forecast,
                              statement_history_start)
from gemini_provider import GeminiProvider, GeminiUnavailable
from auth_roles import require_analyst, require_admin, VALID_SIGNUP_ROLES
from payments_store import CheckoutStore, utc_now
from paypal import PayPalClient, PayPalError
from wallet_store import WalletStore


MAX_AMOUNT = 1_000_000_000
MAX_BODY_BYTES = 2_000_000
# Deeper bodies would exhaust the recursion in canonical(); no valid request nests this far.
MAX_JSON_DEPTH = 64
# An unchanged export must always import, so the import endpoint gets a far larger cap.
MAX_IMPORT_BODY_BYTES = 512 * 1024 * 1024
# The UI loads only its own script and stylesheet, so everything else stays blocked.
CONTENT_SECURITY_POLICY = ("default-src 'self'; frame-ancestors 'none'; base-uri 'none'; "
                           "form-action 'self'")
MAX_BALANCE = 2**53
AUTH_SESSION_TTL_SECONDS = 30 * 24 * 60 * 60
AUTH_LOGIN_MAX_ATTEMPTS = 10
AUTH_SIGNUP_MAX_ATTEMPTS = 20
AUTH_IP_LOGIN_MAX_ATTEMPTS = 60
AUTH_IP_SIGNUP_MAX_ATTEMPTS = 20
AUTH_RATE_WINDOW_SECONDS = 15 * 60
HANDLE_RE = re.compile(r"^[a-z0-9_]{1,20}$")
EMAIL_RE = re.compile(r"^[^\s@]+@[^\s@]+$")
PASSWORD_HASH_RE = re.compile(r"^[0-9a-f]{32}:[0-9a-f]{64}$")
STATUSES = {"pending", "paid", "declined", "cancelled"}
RFC3339_RE = re.compile(
    r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:\d{2})$"
)


class ApiError(Exception):
    def __init__(self, status: int, code: str, message: str = "Request could not be completed"):
        super().__init__(message)
        self.status, self.code, self.message = status, code, message


class RawJsonNumber:
    """Keeps a syntactically valid number when Python cannot represent its magnitude."""
    def __init__(self, token):
        self.token = token


def parse_json_integer(token):
    try:
        return int(token)
    except ValueError:
        return RawJsonNumber(token)


def parse_json_decimal(token):
    try:
        return Decimal(token)
    except (InvalidOperation, ValueError):
        return RawJsonNumber(token)


def add_small_to_decimal_integer(text, delta):
    """Add a bounded digit count to an arbitrarily long signed decimal integer."""
    sign = -1 if text.startswith("-") else 1
    digits = text[1:] if text[:1] in ("+", "-") else text
    digits = digits.lstrip("0") or "0"
    if delta == 0:
        return ("-" if sign < 0 and digits != "0" else "") + digits
    other_sign = -1 if delta < 0 else 1
    other = str(abs(delta))

    def add_abs(left, right):
        out, carry = [], 0
        i, j = len(left) - 1, len(right) - 1
        while i >= 0 or j >= 0 or carry:
            total = carry + (ord(left[i]) - 48 if i >= 0 else 0) + (ord(right[j]) - 48 if j >= 0 else 0)
            out.append(chr(48 + total % 10))
            carry = total // 10
            i, j = i - 1, j - 1
        return "".join(reversed(out))

    def subtract_abs(left, right):
        out, borrow = [], 0
        i, j = len(left) - 1, len(right) - 1
        while i >= 0:
            digit = ord(left[i]) - 48 - borrow - (ord(right[j]) - 48 if j >= 0 else 0)
            borrow = 1 if digit < 0 else 0
            out.append(chr(48 + digit + (10 if digit < 0 else 0)))
            i, j = i - 1, j - 1
        return "".join(reversed(out)).lstrip("0") or "0"

    if sign == other_sign:
        result, result_sign = add_abs(digits, other), sign
    else:
        ordering = (len(digits) > len(other)) - (len(digits) < len(other))
        if ordering == 0:
            ordering = (digits > other) - (digits < other)
        if ordering == 0:
            return "0"
        if ordering > 0:
            result, result_sign = subtract_abs(digits, other), sign
        else:
            result, result_sign = subtract_abs(other, digits), other_sign
    return ("-" if result_sign < 0 and result != "0" else "") + result


def normalize_raw_number(token):
    match = re.fullmatch(r"(-?)(0|[1-9][0-9]*)(?:\.([0-9]+))?(?:[eE]([+-]?[0-9]+))?", token)
    if match is None:
        raise ValueError("Invalid JSON number")
    sign, whole, fraction, exponent = match.groups()
    fraction = fraction or ""
    digits = (whole + fraction).lstrip("0")
    if not digits:
        return ("number", (0, "0", "0"))
    trailing = len(digits) - len(digits.rstrip("0"))
    if trailing:
        digits = digits[:-trailing]
    exponent = add_small_to_decimal_integer(exponent or "0", trailing - len(fraction))
    return ("number", (-1 if sign else 1, digits, exponent))


def fail(status: int, code: str, message: str = "Request could not be completed"):
    raise ApiError(status, code, message)


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


def parse_timestamp(value):
    if not isinstance(value, str) or not RFC3339_RE.fullmatch(value):
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else None


def timestamp_param(query, name):
    if name not in query:
        return None
    raw = query[name][0]
    parsed = parse_timestamp(raw)
    if parsed is None:
        fail(422, "validation_failed", f"{name} must be an RFC 3339 instant with an offset")
    return raw, parsed


def instant_key(value):
    return parse_timestamp(value).astimezone(timezone.utc)


def new_id(prefix: str) -> str:
    return prefix + "_" + secrets.token_hex(12)


def json_too_deep(value, limit=MAX_JSON_DEPTH) -> bool:
    stack = [(value, 1)]
    while stack:
        item, depth = stack.pop()
        if isinstance(item, (list, dict)):
            if depth > limit:
                return True
            stack.extend((child, depth + 1) for child in (item.values() if isinstance(item, dict) else item))
    return False


def canonical(value) -> str:
    def normalize(item):
        if isinstance(item, RawJsonNumber):
            return normalize_raw_number(item.token)
        if item is None:
            return ("null",)
        if isinstance(item, bool):
            return ("boolean", item)
        if isinstance(item, (int, float, Decimal)):
            number = item if isinstance(item, Decimal) else Decimal(str(item))
            if not number.is_finite():
                return ("non-finite-number", str(number))
            sign, digits, exponent = number.as_tuple()
            digits = list(digits)
            if all(digit == 0 for digit in digits):
                return ("number", (0, "0", "0"))
            while len(digits) > 1 and digits[-1] == 0:
                digits.pop()
                exponent += 1
            return ("number", (-1 if sign else 1,
                               "".join(str(digit) for digit in digits), str(exponent)))
        if isinstance(item, str):
            return ("string", item)
        if isinstance(item, list):
            return ("array", tuple(normalize(v) for v in item))
        if isinstance(item, dict):
            return ("object", tuple(sorted((k, normalize(v)) for k, v in item.items())))
        raise ValueError("Request body contains a non-JSON value")
    return repr(normalize(value))


def integer_in_range(value, minimum, maximum):
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value if minimum <= value <= maximum else None
    if isinstance(value, Decimal):
        if (not value.is_finite() or value != value.to_integral_value()
                or value < minimum or value > maximum):
            return None
        return int(value)
    return None


def validate_financial_links(payments, requests):
    payment_by_id = {payment["id"]: payment for payment in payments}
    request_by_id = {request["id"]: request for request in requests}
    for payment in payments:
        request_id = payment.get("request_id")
        settlement_id = payment.get("settlement_id")
        if settlement_id is not None and (not isinstance(settlement_id, str) or
                                           not settlement_id or len(settlement_id) > 64):
            raise ValueError("invalid settlement link")
        if request_id is None:
            continue
        if (not isinstance(request_id, str) or not request_id or len(request_id) > 64 or
                settlement_id is not None):
            raise ValueError("invalid request payment link")
        request = request_by_id.get(request_id)
        if (request is None or request["status"] != "paid" or
                request.get("payment_id") != payment["id"] or
                request["payer_id"] != payment["from_user_id"] or
                request["requester_id"] != payment["to_user_id"] or
                request["amount"] != payment["amount"]):
            raise ValueError("inconsistent request payment link")
    for request in requests:
        payment_id = request.get("payment_id")
        if request["status"] == "paid":
            if not isinstance(payment_id, str) or payment_id not in payment_by_id:
                raise ValueError("paid request has no payment")
            payment = payment_by_id[payment_id]
            if payment.get("request_id") != request["id"]:
                raise ValueError("paid request has the wrong payment")
        elif payment_id is not None:
            raise ValueError("unpaid request has a payment")


def validate_refund_links(payments):
    """Refunds reverse a non-refund payment between the same wallets, within its amount."""
    payment_by_id = {payment["id"]: payment for payment in payments}
    refunded = {}
    for payment in payments:
        target_id = payment.get("refund_of")
        if target_id is None:
            continue
        target = payment_by_id.get(target_id) if isinstance(target_id, str) else None
        if (target is None or target.get("refund_of") is not None
                or payment["from_user_id"] != target["to_user_id"]
                or payment["to_user_id"] != target["from_user_id"]
                or payment["amount"] < 1
                or payment.get("request_id") is not None
                or payment.get("settlement_id") is not None
                or payment.get("authorization_id") is not None):
            raise ValueError("invalid refund link")
        refunded[target_id] = refunded.get(target_id, 0) + payment["amount"]
    for target_id, amount in refunded.items():
        revisions = payment_by_id[target_id].get("revisions") or []
        current = revisions[-1]["amount"] if revisions else payment_by_id[target_id]["amount"]
        if amount > current:
            raise ValueError("refunds exceed payment")


def amount_value(value, *, required=True) -> int:
    if value is None and not required:
        return 0
    if isinstance(value, bool) or not isinstance(value, (int, float, Decimal)):
        fail(422, "validation_failed", "amount must be an integer minor-unit value")
    if isinstance(value, Decimal):
        if not value.is_finite() or value != value.to_integral_value():
            fail(422, "validation_failed", "amount must be an integer minor-unit value")
        if value < 1 or value > MAX_AMOUNT:
            fail(422, "validation_failed", "amount is outside the allowed range")
        value = int(value)
    elif isinstance(value, float):
        if not value.is_integer():
            fail(422, "validation_failed", "amount must be an integer minor-unit value")
        value = int(value)
    if value < 1 or value > MAX_AMOUNT:
        fail(422, "validation_failed", "amount is outside the allowed range")
    return value


def required_string(body, name: str) -> str:
    if name not in body:
        fail(422, "validation_failed", f"{name} is required")
    if not isinstance(body[name], str):
        fail(400, "malformed_request", f"{name} must be a string")
    return body[name]


def optional_note(body) -> str:
    if "note" not in body:
        return ""
    value = body["note"]
    if not isinstance(value, str):
        fail(422, "validation_failed", "note must be a string")
    if len(value) > 200:
        fail(422, "validation_failed", "note exceeds 200 characters")
    return value


def visibility(body) -> str:
    value = body.get("visibility", "public")
    if value not in ("public", "private") or not isinstance(value, str):
        fail(422, "validation_failed", "visibility must be public or private")
    return value


def page_value(query, name, default, minimum, maximum=None):
    if name not in query:
        return default
    raw = query[name][0]
    if not re.fullmatch(r"[0-9]+", raw):
        fail(422, "validation_failed", f"{name} must be decimal digits")
    try:
        value = int(raw)
    except ValueError:
        fail(422, "validation_failed", f"{name} is outside the supported range")
    if value < minimum or (maximum is not None and value > maximum):
        fail(422, "validation_failed", f"{name} is outside the allowed range")
    return value


# Each scrypt call takes ~16 MiB and a full CPU for tens of milliseconds. Capping concurrent
# login/signup hashes keeps a burst of logins from starving every other request of CPU.
AUTH_HASH_SLOTS = threading.BoundedSemaphore(2)


def password_hash(password: str, salt: bytes | None = None) -> str:
    salt = salt or secrets.token_bytes(16)
    digest = hashlib.scrypt(password.encode("utf-8"), salt=salt, n=2**14, r=8, p=1, dklen=32)
    return salt.hex() + ":" + digest.hex()


def password_matches(password: str, encoded: str) -> bool:
    try:
        salt_hex, digest_hex = encoded.split(":", 1)
        salt, expected = bytes.fromhex(salt_hex), bytes.fromhex(digest_hex)
        actual = hashlib.scrypt(password.encode("utf-8"), salt=salt, n=2**14, r=8, p=1, dklen=32)
        return hmac.compare_digest(actual, expected)
    except (ValueError, TypeError):
        return False


def empty_state():
    return {"currency": "EUR", "minor_units": 2, "users": {}, "emails": {}, "handles": {},
            "tokens": {}, "payments": [], "requests": [], "operators": [],
            "idempotency": {}, "seeded_total": 0, "sequence": 0,
            "authorization_ttl_seconds": 600, "authorizations": [],
            "opening_balances": {}}


def require_admin(user):
    """Check if user has PLATFORM_ADMIN role. Returns (has_access, error_message)."""
    if not user:
        return False, "User not found"
    if "PLATFORM_ADMIN" not in user.get("roles", []):
        return False, "Admin access required"
    return True, None


def require_analyst(user):
    """Check if user has PAYMENT_ANALYST role and is approved. Returns (has_access, error_message)."""
    if not user:
        return False, "User not found"
    if "PAYMENT_ANALYST" not in user.get("roles", []):
        return False, "Analyst role required"
    if user.get("analyst_status") != "approved":
        return False, "Analyst account not approved"
    return True, None


class Service:
    def __init__(self):
        # Import and setup database configuration
        from db_config import ensure_data_dir_exists, get_db_paths, get_database_config
        
        db_config = get_database_config()
        self.db_type = db_config['type']
        
        if self.db_type == 'sqlite':
            ensure_data_dir_exists()
            db_paths = get_db_paths()
        
        self.lock = threading.RLock()
        self.state = empty_state()
        self.statement_snapshots = {}
        self.ai_proposals = {}
        self.ai_provider = DeterministicProvider()
        self.gemini_provider = GeminiProvider()
        self.companion_requests = {}
        
        if self.db_type == 'sqlite':
            # Use configured paths or environment variable overrides
            payment_db = os.environ.get("PAYPAL_DB_PATH", db_paths["payments"])
            finance_db = os.environ.get("MORROW_FINANCE_DB_PATH", db_paths["finance"])
            wallet_db = os.environ.get("MORROW_WALLET_DB_PATH", db_paths["wallet"])
            
            # Validate paths are different
            payment_path = Path(payment_db).resolve()
            finance_path = Path(finance_db).resolve()
            wallet_path = Path(wallet_db).resolve()
            if wallet_path in (payment_path, finance_path):
                raise RuntimeError("MORROW_WALLET_DB_PATH must be separate from PayPal and finance databases")
        else:
            # PostgreSQL: paths not used
            payment_db = None
            finance_db = None
            wallet_db = None
        
        self.paypal_store = CheckoutStore(payment_db)
        self.finance_store = FinanceStore(finance_db)
        self.wallet_store = WalletStore(wallet_db)  # Handles None for PostgreSQL
        
        stored_state = self.wallet_store.load()
        migration_file = os.environ.get("MORROW_WALLET_MIGRATION_FILE")
        if stored_state is not None:
            self.state = self._validate_import({"track": "pocketful", "format_version": 1,
                                                "state": stored_state})
            if migration_file:
                raise RuntimeError("Wallet migration file is only allowed before wallet state initialization")
        elif migration_file:
            migration_path = Path(migration_file)
            if not migration_path.is_file() or migration_path.stat().st_size > MAX_IMPORT_BODY_BYTES:
                raise RuntimeError("Configured wallet migration file is missing or too large")
            try:
                document = json.loads(migration_path.read_text(encoding="utf-8"))
                self.state = self._validate_import(document)
            except (OSError, UnicodeDecodeError, json.JSONDecodeError, ApiError) as exc:
                raise RuntimeError("Configured wallet migration file is invalid") from exc
            self.wallet_store.initialize(self.state)
        else:
            self.wallet_store.initialize(self.state)
        with self.wallet_store.transaction() as persisted:
            self.wallet_store.save(persisted)
        self.dummy_password_hash = password_hash("Morrow-unknown-account-dummy")
        self.paypal = PayPalClient()

    @staticmethod
    def _provider_request_id(user_id, key, action):
        return str(uuid.uuid5(uuid.NAMESPACE_URL, f"pocketful:{user_id}:{key}:{action}"))

    def _checkout_obj(self, row, *, include_approval=True):
        result = {"checkout_id": row["checkout_id"], "status": row["status"],
                  "order_id": row["order_id"], "capture_id": row["capture_id"],
                  "amount_minor": row["amount_minor"], "currency": row["currency"],
                  "note": row["note"], "created_at": row["created_at"],
                  "updated_at": row["updated_at"], "version": row["version"],
                  "provider_error": row["provider_error"]}
        result["merchant_label"] = os.environ.get("PAYPAL_MERCHANT_LABEL", "Morrow Sandbox merchant")
        if include_approval and row["approval_url"] and row["status"] in ("CREATED", "CREATE_UNKNOWN"):
            result["approval_url"] = row["approval_url"]
        return result

    def _paypal_error(self, error):
        if error.code == "paypal_not_configured":
            fail(503, "paypal_not_configured", "Set PAYPAL_CLIENT_ID and PAYPAL_CLIENT_SECRET to enable Sandbox checkout")
        if error.code == "webhook_not_configured":
            fail(503, "webhook_not_configured", "Set PAYPAL_WEBHOOK_ID to enable verified webhooks")
        fail(503 if error.retryable else 502, error.code,
             "PayPal Sandbox is temporarily unavailable" if error.retryable else str(error))

    def _start_paypal_checkout(self, uid, body, headers):
        if not self.paypal.configured:
            fail(503, "paypal_not_configured", "Set PAYPAL_CLIENT_ID and PAYPAL_CLIENT_SECRET to enable Sandbox checkout")
        amount = amount_value(body.get("amount_minor", body.get("amount")))
        note = optional_note(body)
        key = headers.get("Idempotency-Key", "")
        if not key:
            fail(400, "missing_idempotency_key", "Idempotency-Key is required")
        if len(key) > 255:
            fail(422, "validation_failed", "Idempotency-Key exceeds 255 characters")
        currency = self.state["currency"]
        minor_units = self.state["minor_units"]
        if (currency, minor_units) not in (("EUR", 2), ("JPY", 0)):
            fail(422, "paypal_unsupported_currency", "PayPal Sandbox checkout supports EUR and JPY in this app")
        request = {"amount_minor": amount, "currency": currency, "note": note,
                   "minor_units": minor_units}
        checkout_id = new_id("pc")
        disposition, row = self.paypal_store.claim(checkout_id, uid, key, request)
        if disposition == "conflict":
            fail(409, "idempotency_key_reuse", "Idempotency-Key was already used for another checkout")
        if row["status"] not in ("CREATING", "CREATE_UNKNOWN"):
            return (200 if disposition == "existing" else 201), self._checkout_obj(row)
        base_url = os.environ.get("POCKETFUL_PUBLIC_URL", f"http://localhost:{os.environ.get('PORT', '8080')}").rstrip("/")
        parsed = urlsplit(base_url)
        if parsed.scheme not in ("http", "https") or not parsed.netloc or (
                parsed.scheme != "https" and parsed.hostname not in ("localhost", "127.0.0.1", "::1")):
            fail(500, "invalid_public_url", "POCKETFUL_PUBLIC_URL must be HTTPS except for localhost")
        return_url = f"{base_url}/paypal/return?checkout_id={row['checkout_id']}"
        cancel_url = f"{base_url}/paypal/return?checkout_id={row['checkout_id']}&cancelled=1"
        request_id = self._provider_request_id(uid, key, "create")
        try:
            order = self.paypal.create_order(checkout_id=row["checkout_id"], amount_minor=amount,
                                             currency=currency, minor_units=minor_units, note=note,
                                             return_url=return_url, cancel_url=cancel_url,
                                             request_id=request_id)
        except PayPalError as exc:
            status = "CREATE_UNKNOWN" if exc.retryable else "FAILED"
            self.paypal_store.transition(row["checkout_id"], status, event="order_creation_failed",
                                         actor=uid, provider_error=exc.code,
                                         details={"retryable": exc.retryable})
            self._paypal_error(exc)
        updated = self.paypal_store.transition(row["checkout_id"], "CREATED", event="order_created",
                                               actor=uid, order_id=order["id"],
                                               approval_url=order["approval_url"],
                                               details={"paypal_order_status": order["status"]})
        return 201, self._checkout_obj(updated)

    @staticmethod
    def _checkout_minor_units(row):
        try:
            return int(json.loads(row["request_json"])["minor_units"])
        except (ValueError, TypeError, KeyError):
            return 2

    @staticmethod
    def _order_capture(order):
        for unit in order.get("purchase_units", []):
            for capture in (unit.get("payments") or {}).get("captures", []):
                if capture.get("id"):
                    return capture
        return None

    def _sync_paypal_order(self, row, order, *, actor=None, event="order_reconciled"):
        if order.get("id") != row["order_id"]:
            fail(502, "paypal_order_mismatch", "PayPal returned a different order")
        capture = self._order_capture(order)
        provider_status = order.get("status")
        target = "CREATED"
        capture_status = capture.get("status") if capture else None
        if capture_status == "REVERSED":
            target = "REVERSED"
        elif capture_status == "REFUNDED":
            target = "REFUNDED"
        elif capture_status == "PARTIALLY_REFUNDED":
            target = "PARTIALLY_REFUNDED"
        elif capture_status in ("DENIED", "DECLINED"):
            target = "FAILED"
        elif capture_status == "PENDING":
            target = "CAPTURE_PENDING"
        elif capture_status == "COMPLETED":
            amount = (capture.get("amount") or {})
            expected = self._paypal_value(row["amount_minor"], self._checkout_minor_units(row))
            if amount.get("currency_code") != row["currency"] or amount.get("value") != expected:
                target = "REVIEW_REQUIRED"
            else:
                target = "COMPLETED"
        elif provider_status == "APPROVED":
            target = "APPROVED"
        elif provider_status == "COMPLETED":
            target = "CAPTURE_PENDING"
        elif provider_status == "VOIDED":
            target = "VOIDED"
        updated = self.paypal_store.transition(row["checkout_id"], target, event=event, actor=actor,
                                               capture_id=capture.get("id") if capture else None,
                                               details={"paypal_order_status": provider_status,
                                                        "paypal_capture_status": capture_status})
        return updated

    @staticmethod
    def _paypal_value(amount_minor, minor_units):
        scale = 10 ** minor_units
        value = str(amount_minor // scale)
        return value if not minor_units else value + "." + str(amount_minor % scale).zfill(minor_units)

    def _capture_paypal_checkout(self, uid, checkout_id, headers):
        key = headers.get("Idempotency-Key", "")
        if not key:
            fail(400, "missing_idempotency_key", "Idempotency-Key is required")
        if len(key) > 255:
            fail(422, "validation_failed", "Idempotency-Key exceeds 255 characters")
        row = self.paypal_store.get(checkout_id)
        if row is None or row["user_id"] != uid:
            fail(404, "not_found", "Checkout not found")
        if row["status"] == "COMPLETED":
            return 200, self._checkout_obj(row, include_approval=False)
        if row["status"] in ("FAILED", "VOIDED", "REVIEW_REQUIRED"):
            fail(409, "checkout_not_capturable", f"Checkout is {row['status'].lower()}")
        if not row["order_id"]:
            fail(409, "order_not_ready", "Retry the same checkout request to recover order creation")
        try:
            current = self.paypal.get_order(row["order_id"])
            row = self._sync_paypal_order(row, current, actor=uid, event="capture_preflight_reconciled")
            if row["status"] == "COMPLETED":
                return 200, self._checkout_obj(row, include_approval=False)
            if current.get("status") != "APPROVED":
                fail(409, "buyer_approval_required", "PayPal has not confirmed buyer approval")
            self.paypal_store.transition(checkout_id, "CAPTURE_IN_PROGRESS", event="capture_started", actor=uid,
                                         details={"request_key_hash": hashlib.sha256(key.encode()).hexdigest()})
            capture_result = self.paypal.capture_order(
                row["order_id"], self._provider_request_id(uid, row["idempotency_key"], "capture"))
        except ApiError:
            raise
        except PayPalError as exc:
            # An uncertain response keeps the same provider request ID available for safe retry.
            self.paypal_store.transition(checkout_id,
                "CAPTURE_UNKNOWN" if exc.retryable else "FAILED", event="capture_failed",
                actor=uid, provider_error=exc.code, details={"retryable": exc.retryable})
            self._paypal_error(exc)
        capture = self._order_capture(capture_result)
        capture_status = capture.get("status") if capture else None
        amount = (capture or {}).get("amount") or {}
        expected = self._paypal_value(row["amount_minor"], self._checkout_minor_units(row))
        if capture_status == "COMPLETED":
            target = ("COMPLETED" if amount.get("currency_code") == row["currency"]
                      and amount.get("value") == expected else "REVIEW_REQUIRED")
        elif capture_status == "PENDING":
            target = "CAPTURE_PENDING"
        else:
            target = "FAILED"
        updated = self.paypal_store.transition(checkout_id, target, event="capture_response_received",
                                               actor=uid, capture_id=capture.get("id") if capture else None,
                                               provider_error=None if target in ("COMPLETED", "CAPTURE_PENDING") else "capture_not_completed",
                                               details={"paypal_capture_status": capture_status})
        if target == "REVIEW_REQUIRED":
            fail(409, "capture_amount_mismatch", "PayPal reported a capture that needs manual review")
        if target == "FAILED":
            fail(502, "capture_not_completed", "PayPal did not complete the capture")
        return 200, self._checkout_obj(updated, include_approval=False)

    def _reconcile_paypal_checkout(self, uid, checkout_id):
        row = self.paypal_store.get(checkout_id)
        if row is None or row["user_id"] != uid:
            fail(404, "not_found", "Checkout not found")
        if not row["order_id"]:
            return 200, self._checkout_obj(row)
        try:
            order = self.paypal.get_order(row["order_id"])
        except PayPalError as exc:
            self._paypal_error(exc)
        updated = self._sync_paypal_order(row, order, actor=uid)
        return 200, self._checkout_obj(updated)

    def _paypal_webhook(self, headers, event, raw_body):
        if not isinstance(event.get("id"), str) or not event.get("id") or not isinstance(event.get("event_type"), str):
            fail(400, "invalid_webhook", "PayPal event must include id and event_type")
        try:
            valid = self.paypal.verify_webhook(headers, event)
        except PayPalError as exc:
            self._paypal_error(exc)
        if not valid:
            fail(400, "invalid_webhook_signature", "PayPal webhook signature is invalid")
        event_type = event["event_type"]
        resource = event.get("resource") or {}
        related = ((resource.get("supplementary_data") or {}).get("related_ids") or {})
        order_id = related.get("order_id") or (resource.get("id") if event_type.startswith("CHECKOUT.ORDER.") else None)
        capture_id = resource.get("id") if event_type.startswith("PAYMENT.CAPTURE.") else None
        inserted, stored = self.paypal_store.record_webhook(
            event_id=event["id"], event_type=event_type, order_id=order_id,
            capture_id=capture_id, payload_hash=hashlib.sha256(raw_body).hexdigest())
        if not inserted and stored["processing_status"] == "PROCESSED":
            return 200, {"received": True, "duplicate": True}
        if event_type not in ("CHECKOUT.ORDER.APPROVED", "PAYMENT.CAPTURE.PENDING",
                              "PAYMENT.CAPTURE.COMPLETED", "PAYMENT.CAPTURE.DENIED",
                              "PAYMENT.CAPTURE.REVERSED", "PAYMENT.CAPTURE.REFUNDED"):
            self.paypal_store.finish_webhook(event["id"], "IGNORED")
            return 200, {"received": True, "ignored": True}
        row = self.paypal_store.get_for_order(order_id) if isinstance(order_id, str) else None
        if row is None:
            # Persist an unmatched verified event for later inspection/reconciliation.
            self.paypal_store.finish_webhook(event["id"], "UNMATCHED")
            return 200, {"received": True, "matched": False}
        try:
            order = self.paypal.get_order(row["order_id"])
            self._sync_paypal_order(row, order, event="verified_webhook_reconciled")
        except PayPalError as exc:
            self._paypal_error(exc)
        self.paypal_store.finish_webhook(event["id"], "PROCESSED")
        return 200, {"received": True, "matched": True}

    def _assistant_proposal(self, uid, body):
        text = body.get("message")
        users = self.state["users"]
        aliases = {u["handle"]: {u["handle"], *u["display_name"].lower().split()}
                   for u in users.values()}
        try:
            proposal = self.ai_provider.propose(
                text, caller_handle=users[uid]["handle"], known_handles=aliases,
                minor_units=self.state["minor_units"], currency=self.state["currency"])
        except IntentError as exc:
            fail(422, "assistant_could_not_parse", str(exc))
        if proposal["action"] == "balance_query":
            other_id = self.state["handles"][proposal.pop("other_handle")]
            # Outstanding social requests are obligations, not completed transfers.
            balance = sum(r["amount"] for r in self.state["requests"]
                          if r["status"] == "pending" and r["requester_id"] == uid
                          and r["payer_id"] == other_id)
            proposal.update({"amount_owed_minor": balance, "currency": self.state["currency"],
                             "explanation": "Sum of your pending requests to this person."})
            return 200, proposal
        proposal_id = new_id("ap")
        self.ai_proposals[proposal_id] = {"user_id": uid, "proposal": proposal,
                                          "expires_at": time.time() + 600}
        response = {"proposal_id": proposal_id, **copy.deepcopy(proposal),
                    "currency": self.state["currency"], "requires_confirmation": True,
                    "expires_in_seconds": 600}
        if proposal["action"] == "paypal_checkout":
            response["merchant_label"] = os.environ.get("PAYPAL_MERCHANT_LABEL", "Morrow Sandbox merchant")
        if proposal["action"] == "split":
            count = len(proposal["participant_handles"])
            q, rem = divmod(proposal["amount"], count)
            response["shares"] = [{"handle": h, "amount": q + (i < rem)}
                                   for i, h in enumerate(proposal["participant_handles"])]
            response["explanation"] = (f"{proposal['amount']} minor units divided among {count} people; "
                                        "remainder minor units go to participants in listed order.")
        else:
            response["explanation"] = "This creates a pending request; it does not move money."
        return 200, response

    def _confirm_assistant_proposal(self, uid, proposal_id, body, headers):
        saved = self.ai_proposals.get(proposal_id)
        if saved is None or saved["user_id"] != uid:
            fail(404, "not_found", "Assistant proposal not found")
        if saved["expires_at"] < time.time():
            self.ai_proposals.pop(proposal_id, None)
            fail(409, "proposal_expired", "This proposal expired. Ask the assistant again.")
        if body != {"confirm": True}:
            fail(422, "confirmation_required", "Explicit confirmation is required")
        path = f"/ai/proposals/{proposal_id}/confirm"
        status, replay, signature = self._key_and_replay(headers, uid, "POST", path, body)
        if status is not None:
            return status, replay
        if saved.get("confirmed"):
            fail(409, "proposal_already_confirmed", "This proposal has already been confirmed.")
        proposal = saved["proposal"]
        if proposal["action"] == "split":
            result_status, result = self._create_split(uid, {
                "amount": proposal["amount"], "participant_handles": proposal["participant_handles"],
                "note": proposal["note"]}, signature)
        elif proposal["action"] == "request":
            result_status, result = self._create_request(uid, {
                "amount": proposal["amount"], "payer_handle": proposal["payer_handle"],
                "note": proposal["note"]}, signature)
        else:
            result_status, result = self._start_paypal_checkout(uid, {
                "amount_minor": proposal["amount"], "note": proposal["note"]},
                {"Idempotency-Key": headers.get("Idempotency-Key", "")})
        response = {"confirmed": True, "action": proposal["action"], "result": result}
        self._save_idempotency(signature, response)
        saved["confirmed"] = True
        saved["result"] = copy.deepcopy(response)
        return result_status, response

    def _idempotency_slot(self, user_id, key, method, path):
        # The client key is scoped by caller and operation path; reuse on a different
        # endpoint is an independent request under the Stage 1 contract.
        return "\0".join((user_id, key, method, path))

    def _get_user(self, uid):
        return self.state["users"].get(uid)

    def _resolve(self, handle):
        if not HANDLE_RE.fullmatch(handle):
            fail(404, "not_found", "No user has that handle")
        uid = self.state["handles"].get(handle)
        if uid is None:
            fail(404, "not_found", "No user has that handle")
        return self.state["users"][uid]

    @staticmethod
    def _finance_fields(body, allowed):
        extra = set(body) - set(allowed)
        if extra:
            fail(422, "validation_failed", "Request contains unsupported fields")

    @staticmethod
    def _finance_key(headers):
        key = headers.get("Idempotency-Key", "")
        if not isinstance(key, str) or not key:
            fail(400, "missing_idempotency_key", "Idempotency-Key is required")
        if len(key) > 255 or any(ord(char) < 32 for char in key):
            fail(422, "validation_failed", "Idempotency-Key is invalid")
        return key

    def _finance_currency(self, body):
        currency = body.get("currency", self.state["currency"])
        if not isinstance(currency, str) or not re.fullmatch(r"[A-Z]{3}", currency):
            fail(422, "validation_failed", "currency must be a three-letter uppercase code")
        if currency != self.state["currency"]:
            fail(422, "currency_mismatch", "Finance records must use this wallet’s currency")
        return currency

    @staticmethod
    def _finance_text(value, field, maximum, *, required=True):
        if not isinstance(value, str):
            fail(422, "validation_failed", f"{field} must be a string")
        value = value.strip()
        if (required and not value) or len(value) > maximum or any(ord(char) < 32 for char in value):
            fail(422, "validation_failed", f"{field} is invalid")
        return value

    @staticmethod
    def _finance_category(value):
        category = Service._finance_text(value, "category", 40)
        category = category.lower()
        if not re.fullmatch(r"[a-z0-9][a-z0-9_-]{0,39}", category):
            fail(422, "validation_failed", "category must use letters, digits, dashes or underscores")
        return category

    @staticmethod
    def _finance_date(value):
        if not isinstance(value, str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
            fail(422, "validation_failed", "next_due_date must be an ISO calendar date")
        try:
            date.fromisoformat(value)
        except ValueError:
            fail(422, "validation_failed", "next_due_date is not a real calendar date")
        return value

    def _finance_call(self, callback):
        try:
            return callback()
        except IdempotencyConflict as exc:
            fail(409, "idempotency_key_reuse", str(exc))
        except FinanceNotFound as exc:
            fail(404, "not_found", str(exc))
        except FinanceConflict as exc:
            fail(409, "finance_conflict", str(exc))
        except FinanceValidation as exc:
            fail(422, "validation_failed", str(exc))

    @staticmethod
    def _commitment_api(row):
        fields = ("commitment_id", "title", "category", "amount_minor", "currency",
                  "frequency", "interval_days", "next_due_date", "status", "created_at",
                  "updated_at", "version")
        result = {key: row[key] for key in fields if key in row}
        if "occurrences" in row:
            occurrences = []
            for occurrence in row["occurrences"]:
                allowed = ("occurrence_id", "occurrence_number", "due_date", "amount_minor",
                           "status", "display_status", "paid_at", "payment_reference", "created_at")
                occurrences.append({key: occurrence[key] for key in allowed if key in occurrence})
            result["occurrences"] = occurrences
        return result

    @staticmethod
    def _shared_expense_api(row):
        fields = ("expense_id", "description", "category", "gross_amount_minor",
                  "personal_share_minor", "currency", "created_at", "updated_at", "version",
                  "status", "total_receivable_minor", "total_reimbursed_minor",
                  "outstanding_receivable_minor")
        result = {key: row[key] for key in fields if key in row}
        if "participants" in row:
            result["participants"] = []
            for participant in row["participants"]:
                fields = ("participant_handle", "share_minor", "reimbursed_minor",
                          "outstanding_minor", "status")
                result["participants"].append({key: participant[key] for key in fields
                                                if key in participant})
        return result

    def _create_commitment(self, uid, body, headers):
        self._finance_fields(body, {"title", "category", "amount_minor", "currency",
                                    "frequency", "interval_days", "next_due_date"})
        title = self._finance_text(body.get("title"), "title", 80)
        category = self._finance_category(body.get("category"))
        amount = amount_value(body.get("amount_minor"))
        currency = self._finance_currency(body)
        frequency = body.get("frequency")
        if frequency not in ("weekly", "monthly", "annual", "custom"):
            fail(422, "validation_failed", "frequency must be weekly, monthly, annual or custom")
        interval_days = body.get("interval_days")
        if frequency == "custom":
            interval_days = integer_in_range(interval_days, 1, 3660)
            if interval_days is None:
                fail(422, "validation_failed", "custom frequency requires interval_days from 1 to 3660")
        elif interval_days is not None:
            fail(422, "validation_failed", "interval_days is only valid for custom frequency")
        values = {"title": title, "category": category, "amount_minor": amount,
                  "currency": currency, "frequency": frequency, "interval_days": interval_days,
                  "next_due_date": self._finance_date(body.get("next_due_date"))}
        status, row = self._finance_call(lambda: self.finance_store.create_commitment(
            uid, values, self._finance_key(headers)))
        return status, self._commitment_api(row)

    def _update_commitment(self, uid, commitment_id, body, headers):
        allowed = {"title", "category", "amount_minor", "currency", "frequency",
                   "interval_days", "next_due_date"}
        self._finance_fields(body, allowed)
        current = self._finance_call(lambda: self.finance_store.get_commitment(uid, commitment_id))
        values = {}
        if "currency" in body and self._finance_currency(body) != current["currency"]:
            fail(422, "currency_mismatch", "A commitment’s currency cannot be changed")
        if "title" in body: values["title"] = self._finance_text(body["title"], "title", 80)
        if "category" in body: values["category"] = self._finance_category(body["category"])
        if "amount_minor" in body: values["amount_minor"] = amount_value(body["amount_minor"])
        if "next_due_date" in body: values["next_due_date"] = self._finance_date(body["next_due_date"])
        frequency = body.get("frequency", current["frequency"])
        if "frequency" in body and frequency not in ("weekly", "monthly", "annual", "custom"):
            fail(422, "validation_failed", "frequency must be weekly, monthly, annual or custom")
        if "frequency" in body:
            values["frequency"] = frequency
            if frequency == "custom":
                interval = body.get("interval_days", current["interval_days"] if current["frequency"] == "custom" else None)
                interval = integer_in_range(interval, 1, 3660)
                if interval is None:
                    fail(422, "validation_failed", "custom frequency requires interval_days from 1 to 3660")
                values["interval_days"] = interval
            else:
                if body.get("interval_days") is not None:
                    fail(422, "validation_failed", "interval_days is only valid for custom frequency")
                values["interval_days"] = None
        elif "interval_days" in body:
            if current["frequency"] != "custom":
                fail(422, "validation_failed", "interval_days is only valid for custom frequency")
            interval = integer_in_range(body["interval_days"], 1, 3660)
            if interval is None:
                fail(422, "validation_failed", "interval_days must be from 1 to 3660")
            values["interval_days"] = interval
        status, row = self._finance_call(lambda: self.finance_store.update_commitment(
            uid, commitment_id, values, self._finance_key(headers)))
        return status, self._commitment_api(row)

    def _commitment_status(self, uid, commitment_id, action, body, headers):
        self._finance_fields(body, set())
        status, row = self._finance_call(lambda: self.finance_store.set_commitment_status(
            uid, commitment_id, action, self._finance_key(headers)))
        return status, self._commitment_api(row)

    def _pay_commitment_occurrence(self, uid, commitment_id, occurrence_id, body, headers):
        self._finance_fields(body, {"payment_reference"})
        reference = body.get("payment_reference")
        if reference is not None:
            reference = self._finance_text(reference, "payment_reference", 128)
        status, row = self._finance_call(lambda: self.finance_store.pay_occurrence(
            uid, commitment_id, occurrence_id, reference, self._finance_key(headers)))
        return status, self._commitment_api({"occurrences": [row]})["occurrences"][0]

    def _create_shared_expense(self, uid, body, headers):
        self._finance_fields(body, {"description", "category", "gross_amount_minor",
                                    "personal_share_minor", "currency", "participants"})
        description = self._finance_text(body.get("description"), "description", 160)
        category = self._finance_category(body.get("category"))
        gross = amount_value(body.get("gross_amount_minor"))
        personal = integer_in_range(body.get("personal_share_minor"), 0, gross)
        if personal is None:
            fail(422, "validation_failed", "personal_share_minor must be a non-negative integer no greater than the gross amount")
        currency = self._finance_currency(body)
        participants = body.get("participants")
        if not isinstance(participants, list) or not 1 <= len(participants) <= 50:
            fail(422, "validation_failed", "participants must contain between 1 and 50 people")
        normalized, seen = [], set()
        for participant in participants:
            if not isinstance(participant, dict):
                fail(422, "validation_failed", "each participant must be an object")
            self._finance_fields(participant, {"handle", "share_minor"})
            handle = self._finance_text(participant.get("handle"), "participant handle", 20)
            user = self._resolve(handle)
            if user["id"] == uid:
                fail(422, "validation_failed", "the payer’s personal share is supplied separately")
            if user["id"] in seen:
                fail(422, "validation_failed", "participant handles must be unique")
            seen.add(user["id"])
            share = amount_value(participant.get("share_minor"))
            normalized.append({"user_id": user["id"], "handle": user["handle"], "share_minor": share})
        if personal + sum(item["share_minor"] for item in normalized) != gross:
            fail(422, "validation_failed", "personal share plus participant shares must equal gross_amount_minor")
        values = {"description": description, "category": category,
                  "gross_amount_minor": gross, "personal_share_minor": personal,
                  "currency": currency, "participants": normalized}
        status, row = self._finance_call(lambda: self.finance_store.create_shared_expense(
            uid, values, self._finance_key(headers)))
        return status, self._shared_expense_api(row)

    def _settle_shared_expense(self, uid, expense_id, body, headers):
        self._finance_fields(body, {"participant_handle", "amount_minor", "payment_reference"})
        handle = self._finance_text(body.get("participant_handle"), "participant_handle", 20)
        participant = self._resolve(handle)
        reference = body.get("payment_reference")
        if reference is not None:
            reference = self._finance_text(reference, "payment_reference", 128)
        amount = amount_value(body.get("amount_minor"))
        status, result = self._finance_call(lambda: self.finance_store.settle_shared_expense(
            uid, expense_id, participant["id"], amount, reference, self._finance_key(headers)))
        settlement = result["settlement"]
        return status, {"settlement": {"settlement_id": settlement["settlement_id"],
                                       "participant_handle": handle,
                                       "amount_minor": settlement["amount_minor"],
                                       "source": settlement["source"],
                                       "payment_reference": settlement["payment_reference"],
                                       "created_at": settlement["created_at"]},
                        "expense": self._shared_expense_api(result["expense"])}

    def _forecast_statement_history(self, uid, read_at):
        today = read_at.astimezone(timezone.utc).date()
        start = statement_history_start(today)
        lower = datetime(start.year, start.month, start.day, tzinfo=timezone.utc).isoformat()
        query = {"from": [lower], "to": [read_at.isoformat()], "limit": ["200"], "offset": ["0"]}
        page = self._statement(uid, query, read_at)
        snapshot = page["snapshot"]
        entries = list(page["entries"])
        complete = True
        offset = len(entries)
        try:
            while page["has_more"]:
                if len(entries) >= 10000:
                    complete = False
                    break
                page = self._statement(uid, {"snapshot": [snapshot], "limit": ["200"],
                                             "offset": [str(offset)]}, read_at)
                if not page["entries"] and page["has_more"]:
                    complete = False
                    break
                entries.extend(page["entries"])
                offset += len(page["entries"])
        finally:
            self.statement_snapshots.pop(snapshot, None)
        if len(entries) > 10000:
            entries = entries[:10000]
            complete = False
        return entries, complete

    def _calculate_financial_outlook(self, uid, read_at):
        preferences = self.finance_store.ensure_financial_preferences(uid)
        today = read_at.astimezone(timezone.utc).date()
        month_end = date(today.year, today.month,
                         calendar.monthrange(today.year, today.month)[1])
        entries, history_complete = self._forecast_statement_history(uid, read_at)
        unpaid = self.finance_store.unpaid_commitment_occurrences(uid, month_end.isoformat())
        paid_references = self.finance_store.paid_commitment_references(uid)
        result, inputs = calculate_forecast(
            today=today,
            coverage_started_at=preferences["history_coverage_started_at"],
            entries=entries,
            unpaid_occurrences=unpaid,
            paid_payment_references=paid_references,
            available_funds_minor=self._available(uid),
            savings_target_minor=preferences["savings_target_minor"],
            safety_buffer_minor=preferences["safety_buffer_minor"],
            currency=self.state["currency"],
            minor_units=self.state["minor_units"],
            history_complete=history_complete)
        self.finance_store.save_forecast_snapshot(
            uid, result["period_start"], result["period_end"],
            result["calculation_date"], MODEL_VERSION, inputs, result)
        return result

    @staticmethod
    def _assistant_category(value):
        known = {"housing", "rent", "utilities", "subscriptions", "transport",
                 "food", "groceries", "health", "education", "insurance",
                 "entertainment", "other"}
        if isinstance(value, str) and value.strip().lower() in known:
            return value.strip().lower()
        return None

    def _financial_companion_context(self, uid, read_at, question):
        today = read_at.astimezone(timezone.utc).date()
        me = self._me_at(uid, {}, read_at)
        outlook = self._calculate_financial_outlook(uid, read_at)
        due_through = (today + timedelta(days=30)).isoformat()
        upcoming = self.finance_store.unpaid_commitment_occurrences(uid, due_through)
        shared = self.finance_store.shared_expense_summary(uid)
        owed = sum(item["amount"] for item in self.state["requests"]
                   if item["status"] == "pending" and item["payer_id"] == uid)
        request_receivable = sum(item["amount"] for item in self.state["requests"]
                                 if item["status"] == "pending" and item["requester_id"] == uid)

        start = today.replace(day=1)
        query = {"from": [datetime(start.year, start.month, start.day,
                                   tzinfo=timezone.utc).isoformat()],
                 "to": [read_at.isoformat()], "limit": ["200"], "offset": ["0"]}
        statement = self._statement(uid, query, read_at)
        recent = []
        totals_by_category = {}
        for entry in statement["entries"][-20:]:
            payment = entry["payment"]
            category = self._assistant_category(payment.get("category"))
            amount = abs(int(entry["delta"]))
            recent.append({"date": entry["effective_at"][:10],
                           "direction": "outgoing" if entry["delta"] < 0 else "incoming",
                           "amount_minor": amount, "category": category})
            if entry["delta"] < 0 and category:
                totals_by_category[category] = totals_by_category.get(category, 0) + amount
        self.statement_snapshots.pop(statement.get("snapshot"), None)
        context = {
            "currency": me["currency"], "minor_units": me["minor_units"],
            "wallet": {"balance_minor": me["total"], "available_after_holds_minor": me["available"],
                       "holds_minor": me["held"]},
            "safe_to_spend": {"status": outlook["status"],
                               "amount_minor": outlook["safe_to_spend_minor"],
                               "unavailable_reasons": outlook["unavailable_reasons"],
                               "calculation_date": outlook["calculation_date"]},
            "forecast": {"period_start": outlook["period_start"],
                         "period_end": outlook["period_end"],
                         "remaining_variable_spend_minor": outlook["forecast_remaining_variable_spend_minor"],
                         "range_minor": outlook["forecast_range_minor"],
                         "confidence": outlook["confidence"],
                         "assumptions": outlook["assumptions"]},
            "upcoming_commitments": [{"due_date": item["due_date"],
                                      "amount_minor": item["amount_minor"],
                                      "category": self._assistant_category(item.get("category"))}
                                     for item in upcoming[:20]],
            "shared_expenses": shared,
            "pending_p2p_requests": {"amount_owed_minor": owed,
                                     "amount_receivable_minor": request_receivable},
            "transactions": {"period_start": start.isoformat(), "recent": recent,
                             "complete": not statement["has_more"],
                             "category_status": "available" if totals_by_category and all(
                                 item.get("category") for item in recent if item["direction"] == "outgoing")
                                 else "unavailable",
                             "outgoing_by_category_minor": totals_by_category,
                             "note": "Descriptions and counterparty identifiers are omitted."},
        }
        hypothetical = re.search(
            r"\b(?:spend|pay|buy|purchase)\s+(?:(?:[$€£¥₹]|rs\.?\s*)\s*)?"
            r"([0-9][0-9,]*(?:\.[0-9]+)?)\b", question, re.I)
        if hypothetical:
            try:
                amount = Decimal(hypothetical.group(1).replace(",", "")) * (10 ** me["minor_units"])
            except InvalidOperation:
                amount = Decimal("-1")
            if amount >= 0 and amount == amount.to_integral_value():
                context["hypothetical_scenario"] = {
                    "type": "additional_spend",
                    "amount_minor": int(amount),
                    "resulting_safe_to_spend_minor": (
                        outlook["safe_to_spend_minor"] - int(amount)
                        if outlook["status"] == "available" else None),
                    "status": outlook["status"],
                }
        return context

    @staticmethod
    def _companion_fallback(context, reason):
        if context["safe_to_spend"]["status"] != "available":
            reasons = context["safe_to_spend"].get("unavailable_reasons", [])
            descriptions = {
                "insufficient_complete_history": "Morrow needs at least three complete months of wallet history.",
                "statement_history_incomplete": "The wallet statement history is incomplete.",
                "wallet_history_coverage_unknown": "The start of wallet history is unknown.",
            }
            explanation = " ".join(descriptions.get(item, "A required financial input is unavailable.")
                                   for item in reasons) or "A required financial input is unavailable."
            return "Safe-to-Spend is unavailable. " + explanation
        if reason == "provider_unavailable":
            return "Gemini is temporarily unavailable. The verified wallet and forecast summary below remains available; Morrow will not estimate or change those figures."
        if reason == "provider_unconfigured":
            return "Gemini is not configured. The verified wallet and forecast summary below remains available."
        if reason == "rate_limited":
            return "Gemini is rate-limited right now. The verified wallet and forecast summary below remains available."
        if reason in ("malformed_response", "ungrounded_numeric_claim"):
            return "Morrow could not verify the AI explanation, so it was discarded. The verified financial summary below remains available."
        return "I can help explain the verified wallet and forecast summary below. The figures are calculated by Morrow, not by the AI provider."

    def _financial_companion(self, uid, body, read_at):
        if set(body) != {"message"} or not isinstance(body.get("message"), str):
            fail(422, "validation_failed", "message must be a string up to 1,000 characters")
        question = body["message"].strip()
        if not question or len(question) > 1000:
            fail(422, "validation_failed", "message must be a string up to 1,000 characters")
        recent = [item for item in self.companion_requests.get(uid, [])
                  if read_at.timestamp() - item < 60]
        if len(recent) >= 10:
            fail(429, "rate_limited", "Please wait before asking another financial question")
        recent.append(read_at.timestamp())
        self.companion_requests[uid] = recent
        context = self._financial_companion_context(uid, read_at, question)

        # Context is snapshotted under the service lock. The bounded provider call runs
        # without it so an upstream timeout cannot stall wallet or payment requests.
        self.lock.release()
        try:
            answer = self.gemini_provider.answer(question, context)
            provider = "gemini"
        except GeminiUnavailable as exc:
            reason = str(exc)
            answer = self._companion_fallback(context, reason)
            provider = "deterministic_fallback"
        finally:
            self.lock.acquire()
        if context["safe_to_spend"]["status"] != "available":
            mandatory = self._companion_fallback(context, "")
            if not answer.startswith("Safe-to-Spend is unavailable."):
                answer = f"{mandatory} {answer}"
        return 200, {"answer": answer, "provider": provider,
                     "financial_context": context, "generated_at": read_at.isoformat(),
                     "disclaimer": "Financial amounts and forecasts are deterministic Morrow data; AI text is explanatory only."}

    @staticmethod
    def _financial_preferences_api(row, currency, minor_units):
        return {"savings_target_minor": row["savings_target_minor"],
                "safety_buffer_minor": row["safety_buffer_minor"],
                "currency": currency, "minor_units": minor_units,
                "updated_at": row["updated_at"], "version": row["version"]}

    def _update_financial_preferences(self, uid, body, headers):
        allowed = {"savings_target_minor", "safety_buffer_minor"}
        self._finance_fields(body, allowed)
        if not body:
            fail(422, "validation_failed", "Provide a savings target or safety buffer")
        values = {}
        for name in allowed.intersection(body):
            value = integer_in_range(body[name], 0, MAX_AMOUNT)
            if value is None:
                fail(422, "validation_failed", f"{name} must be a non-negative integer minor-unit amount")
            values[name] = value
        self.finance_store.ensure_financial_preferences(uid)
        _status, row = self._finance_call(lambda: self.finance_store.update_financial_preferences(
            uid, values, self._finance_key(headers)))
        return self._financial_preferences_api(row, self.state["currency"], self.state["minor_units"])

    def _request_obj(self, item):
        return {"request_id": item["id"], "requester_id": item["requester_id"],
                "requester_handle": self.state["users"][item["requester_id"]]["handle"],
                "payer_id": item["payer_id"],
                "payer_handle": self.state["users"][item["payer_id"]]["handle"],
                "amount": item["amount"], "currency": self.state["currency"],
                "note": item["note"], "status": item["status"],
                "payment_id": item.get("payment_id"), "created_at": item["created_at"]}

    def _payment_obj(self, item):
        return {"payment_id": item["id"], "from_user_id": item["from_user_id"],
                "from_handle": self.state["users"][item["from_user_id"]]["handle"],
                "to_user_id": item["to_user_id"],
                "to_handle": self.state["users"][item["to_user_id"]]["handle"],
                "amount": item["amount"], "currency": self.state["currency"],
                "note": item["note"], "visibility": item["visibility"],
                "request_id": item.get("request_id"), "settlement_id": item.get("settlement_id"),
                "authorization_id": item.get("authorization_id"),
                "refund_of": item.get("refund_of"),
                "created_at": item["created_at"]}

    def _initial_revision(self, amount, created_at):
        return {"revision": 1, "amount": amount, "effective_at": created_at,
                "recorded_at": created_at, "reason": ""}

    def _payment_revisions(self, item, known_at=None):
        revisions = item.get("revisions") or [self._initial_revision(item["amount"], item["created_at"])]
        if known_at is None:
            return revisions
        known = parse_timestamp(known_at)
        return [revision for revision in revisions
                if parse_timestamp(revision["recorded_at"]) <= known]

    def _selected_revision(self, item, known_at=None, revision_overrides=None):
        revisions = (revision_overrides or {}).get(item["id"])
        if revisions is None:
            revisions = self._payment_revisions(item, known_at)
        elif known_at is not None:
            known = parse_timestamp(known_at)
            revisions = [revision for revision in revisions
                         if parse_timestamp(revision["recorded_at"]) <= known]
        return revisions[-1] if revisions else None

    def _opening_balance(self, uid):
        return self.state["opening_balances"].get(uid, 0)

    def _historical_total(self, uid, at, known_at=None, *, inclusive=True,
                          revision_overrides=None):
        balance = self._opening_balance(uid)
        at_dt = parse_timestamp(at) if isinstance(at, str) else at
        for payment in self.state["payments"]:
            revision = self._selected_revision(payment, known_at, revision_overrides)
            effective = parse_timestamp(revision["effective_at"]) if revision else None
            if (revision is None or effective > at_dt
                    or (not inclusive and effective == at_dt)):
                continue
            if payment["from_user_id"] == uid:
                balance -= revision["amount"]
            elif payment["to_user_id"] == uid:
                balance += revision["amount"]
        return balance

    def _hold_steps(self, authorization, payments_by_id):
        """Lifecycle steps of one hold as (event_at, known_at, kind, amount), oldest first.

        Creation comes first so a same-instant release of a seeded closed hold applies
        after it; the stable sort keeps insertion order for ties.
        """
        created = parse_timestamp(authorization["created_at"])
        steps = [(created, created, "create", authorization["amount"])]
        events = authorization.get("capture_events")
        if events is None:
            linked = [p for p in payments_by_id.values()
                      if p.get("authorization_id") == authorization["id"]]
            linked.sort(key=lambda p: (parse_timestamp(p["created_at"]), p["id"]))
            events = [{"payment_id": p["id"], "final":
                       authorization.get("status") == "captured" and p["id"] == authorization.get("payment_id")}
                      for p in linked]
        for event in events:
            payment = payments_by_id.get(event.get("payment_id"))
            if payment is None:
                continue
            event_at = parse_timestamp(payment["created_at"])
            steps.append((event_at, event_at, "release" if event.get("final") else "capture",
                          payment["amount"]))
        status = authorization.get("status")
        closed_at = parse_timestamp(authorization.get("closed_at"))
        if status == "voided" and closed_at is not None:
            steps.append((closed_at, closed_at, "release", 0))
        elif status in ("captured", "voided") and closed_at is None:
            steps.append((created, created, "release", 0))
        # The expiry deadline is known as soon as the creation is known.
        steps.append((parse_timestamp(authorization["expires_at"]), created, "release", 0))
        # A seeded closed hold without created_at is created at reset, possibly after its
        # recorded close. Nothing happens to a hold before it exists, so such steps take
        # effect at creation; the hold then releases there instead of staying open forever.
        steps = [steps[0]] + [(max(event_at, created), max(known_at, created), kind, amount)
                              for event_at, known_at, kind, amount in steps[1:]]
        steps.sort(key=lambda step: step[0])
        return steps

    @staticmethod
    def _apply_hold_step(held, kind, amount):
        if kind == "create":
            return amount
        if kind == "capture":
            return max(0, held - amount)
        return 0

    def _authorization_hold_at(self, authorization, at, known_at, payments_by_id):
        held = 0
        for event_at, step_known_at, kind, amount in self._hold_steps(authorization, payments_by_id):
            if event_at > at or step_known_at > known_at:
                continue
            held = self._apply_hold_step(held, kind, amount)
        return held

    def _historical_held(self, uid, at, known_at=None):
        known = parse_timestamp(known_at) if known_at else datetime.max.replace(tzinfo=timezone.utc)
        payments_by_id = {p["id"]: p for p in self.state["payments"]}
        return sum(self._authorization_hold_at(a, at, known, payments_by_id)
                   for a in self.state["authorizations"] if a["from_user_id"] == uid)

    def _record_historical_state_is_solvent(self, revision_overrides=None, *, include_holds=True):
        """Sweep every effective/event boundary under the latest revisions.

        Balances are checked after all movements at one instant have been combined.
        """
        changes = {}

        def move(at, uid, total=0, held=0):
            slot = changes.setdefault(at, {})
            prior_total, prior_held = slot.get(uid, (0, 0))
            slot[uid] = (prior_total + total, prior_held + held)

        for payment in self.state["payments"]:
            revisions = (revision_overrides or {}).get(payment["id"])
            if revisions is None:
                revisions = self._payment_revisions(payment)
            revision = revisions[-1]
            if revision["amount"]:
                at = parse_timestamp(revision["effective_at"])
                move(at, payment["from_user_id"], total=-revision["amount"])
                move(at, payment["to_user_id"], total=revision["amount"])
        if include_holds:
            payments_by_id = {p["id"]: p for p in self.state["payments"]}
            for authorization in self.state["authorizations"]:
                held = 0
                for event_at, _, kind, amount in self._hold_steps(authorization, payments_by_id):
                    updated = self._apply_hold_step(held, kind, amount)
                    if updated != held:
                        move(event_at, authorization["from_user_id"], held=updated - held)
                        held = updated
        totals = {uid: self._opening_balance(uid) for uid in self.state["users"]}
        holds = {uid: 0 for uid in self.state["users"]}
        for at in sorted(changes):
            for uid, (total, held) in changes[at].items():
                totals[uid] += total
                holds[uid] += held
            for uid in changes[at]:
                if totals[uid] < 0 or totals[uid] - holds[uid] < 0:
                    return False
        return True

    def _append_payment(self, payment):
        payment["revisions"] = [self._initial_revision(payment["amount"], payment["created_at"])]
        self.state["payments"].append(payment)
        return payment

    def _correct_payment(self, uid, payment_id, body, headers):
        payment = next((p for p in self.state["payments"] if p["id"] == payment_id), None)
        if payment is None:
            fail(404, "not_found", "Payment not found")
        if payment["from_user_id"] != uid:
            fail(403, "forbidden", "Only the original sender may correct this payment")
        self._check_correctable(payment, allow_settlement=False)

        status, replay, signature = self._key_and_replay(
            headers, uid, "POST",
            f"/payments/{payment_id}/corrections", body)
        if status is not None:
            return status, replay

        plan = self._plan_correction(payment, body, datetime.now(timezone.utc))
        response = self._commit_corrections([plan])["revisions"][0]
        self._save_idempotency(signature, response)
        return 201, response

    def _find_payment(self, payment_id):
        payment = next((p for p in self.state["payments"] if p["id"] == payment_id), None)
        if payment is None:
            fail(404, "not_found", "Payment not found")
        return payment

    def _refunded_amount(self, payment_id):
        return sum(p["amount"] for p in self.state["payments"] if p.get("refund_of") == payment_id)

    def _check_correctable(self, payment, *, allow_settlement):
        if (payment.get("authorization_id") is not None or payment.get("refund_of") is not None
                or (payment.get("settlement_id") is not None and not allow_settlement)):
            fail(422, "linked_payment_immutable", "Linked payments cannot be corrected")

    def _plan_correction(self, payment, body, request_started):
        """Validate one correction against the payment's current revision."""
        expected_revision = body.get("expected_revision")
        amount = integer_in_range(body.get("amount"), 0, MAX_AMOUNT)
        effective_at = body.get("effective_at")
        reason = body.get("reason")
        effective = parse_timestamp(effective_at)
        if (isinstance(expected_revision, bool) or not isinstance(expected_revision, int)
                or expected_revision < 1 or amount is None
                or effective is None or effective > request_started
                or not isinstance(reason, str) or not 1 <= len(reason) <= 200):
            fail(422, "validation_failed", "Invalid payment correction")
        revisions = self._payment_revisions(payment)
        if expected_revision != len(revisions):
            fail(409, "stale_revision", "Payment revision is stale")
        if amount < self._refunded_amount(payment["id"]):
            fail(422, "refund_exceeds_payment", "Payment would be smaller than its refunds")
        return {"payment": payment, "revisions": revisions, "amount": amount,
                "effective_at": effective_at, "reason": reason}

    def _commit_corrections(self, plans, batch_id=None):
        """Check the combined effect of validated corrections, then apply them atomically.

        Current available funds are checked before historical balances. Every new revision
        shares one recorded_at, later than the latest recorded revision of every payment.
        """
        changes = {}
        for plan in plans:
            payment = plan["payment"]
            delta = plan["amount"] - plan["revisions"][-1]["amount"]
            changes[payment["from_user_id"]] = changes.get(payment["from_user_id"], 0) - delta
            changes[payment["to_user_id"]] = changes.get(payment["to_user_id"], 0) + delta
        balances = {uid: self.state["users"][uid]["balance"] + change
                    for uid, change in changes.items()}
        if any(change < 0 and balances[uid] - self._held(uid) < 0
               for uid, change in changes.items()):
            fail(409, "insufficient_funds", "Insufficient available funds")
        if any(value > MAX_BALANCE for value in balances.values()):
            fail(422, "validation_failed", "Resulting balance exceeds the supported range")

        recorded_dt = datetime.now(timezone.utc)
        for plan in plans:
            last_recorded = parse_timestamp(plan["revisions"][-1]["recorded_at"])
            if recorded_dt <= last_recorded:
                recorded_dt = last_recorded + timedelta(microseconds=1)
        recorded_at = recorded_dt.isoformat()
        overrides, created = {}, []
        for plan in plans:
            revision = {"revision": len(plan["revisions"]) + 1, "amount": plan["amount"],
                        "effective_at": plan["effective_at"], "recorded_at": recorded_at,
                        "reason": plan["reason"]}
            if batch_id is not None:
                revision["correction_batch_id"] = batch_id
            overrides[plan["payment"]["id"]] = plan["revisions"] + [revision]
            created.append(revision)
        if not self._record_historical_state_is_solvent(overrides):
            fail(409, "historical_overdraft", "Correction would make a historical balance negative")

        for uid, value in balances.items():
            self.state["users"][uid]["balance"] = value
        revisions = []
        for plan, revision in zip(plans, created):
            plan["payment"]["revisions"] = overrides[plan["payment"]["id"]]
            revisions.append({"payment_id": plan["payment"]["id"], **revision})
        return {"recorded_at": recorded_at, "revisions": revisions}

    def _correct_batch(self, uid, body, signature):
        if uid not in self.state["operators"]:
            fail(403, "forbidden", "Settlement operator permission required")
        items = body.get("corrections")
        if (not isinstance(items, list) or not 1 <= len(items) <= 32
                or any(not isinstance(item, dict) for item in items)):
            fail(422, "validation_failed", "corrections must contain 1 to 32 objects")
        payment_ids = [item.get("payment_id") for item in items]
        if (any(not isinstance(pid, str) for pid in payment_ids)
                or len(set(payment_ids)) != len(payment_ids)):
            fail(422, "validation_failed", "corrections need distinct payment_id strings")

        request_started = datetime.now(timezone.utc)
        plans = []
        for item in items:
            payment = self._find_payment(item["payment_id"])
            self._check_correctable(payment, allow_settlement=True)
            plans.append(self._plan_correction(payment, item, request_started))

        members = {}
        for plan in plans:
            settlement_id = plan["payment"].get("settlement_id")
            if settlement_id is not None:
                members.setdefault(settlement_id, []).append(plan)
        included = set(payment_ids)
        for settlement_id in members:
            if any(p["id"] not in included for p in self.state["payments"]
                   if p.get("settlement_id") == settlement_id):
                fail(422, "incomplete_settlement", "Every settlement member must be corrected together")
        for plans_in_settlement in members.values():
            if len({parse_timestamp(plan["effective_at"]) for plan in plans_in_settlement}) != 1:
                fail(422, "validation_failed", "Settlement members need one effective instant")

        batch_id = new_id("cb")
        committed = self._commit_corrections(plans, batch_id)
        response = {"correction_batch_id": batch_id, "recorded_at": committed["recorded_at"],
                    "revisions": committed["revisions"]}
        self._save_idempotency(signature, response)
        return 201, response

    def _refund_payment(self, uid, payment_id, body, signature):
        payment = self._find_payment(payment_id)
        if payment["to_user_id"] != uid:
            fail(403, "forbidden", "Only the original receiver may refund this payment")
        if payment.get("refund_of") is not None:
            fail(422, "invalid_refund_target", "A refund cannot be refunded")
        amount = amount_value(body.get("amount"))
        if self._refunded_amount(payment_id) + amount > self._payment_revisions(payment)[-1]["amount"]:
            fail(422, "refund_exceeds_payment", "Refunds would exceed the payment amount")
        refund = self._make_payment(uid, payment["from_user_id"], amount, payment["note"],
                                    payment["visibility"], refund_of=payment_id)
        response = self._payment_obj(refund)
        self._save_idempotency(signature, response)
        return 201, response

    def _me_at(self, uid, query, read_at):
        as_of = timestamp_param(query, "as_of")
        known_at = timestamp_param(query, "known_at")
        user = self.state["users"][uid]
        if as_of is None and known_at is None:
            # Without temporal parameters the live ledger already holds the current
            # corrected values, exactly as stage 2 reported them.
            total, held = user["balance"], self._held(uid)
        else:
            known = min(known_at[1], read_at) if known_at else read_at
            at = as_of[1] if as_of else read_at
            known_filter = known.isoformat()
            total = self._historical_total(uid, at, known_filter)
            held = self._historical_held(uid, at, known_filter)
        result = {"user_id": uid, "display_name": user["display_name"],
                  "handle": user["handle"], "balance": total, "total": total,
                  "available": total - held, "held": held,
                  "currency": self.state["currency"], "minor_units": self.state["minor_units"],
                  "roles": user.get("roles", ["CUSTOMER"])}
        if as_of:
            result["as_of"] = as_of[0]
        if known_at:
            result["known_at"] = known_at[0]
        return result

    def _statement(self, uid, query, read_at):
        limit = page_value(query, "limit", 50, 1, 200)
        offset = page_value(query, "offset", 0, 0)
        snapshot_tokens = query.get("snapshot")
        if snapshot_tokens:
            if any(name in query for name in ("from", "to", "known_at")):
                fail(422, "validation_failed", "A snapshot cannot be combined with a window or known_at")
            snapshot = self.statement_snapshots.get(snapshot_tokens[0])
            if snapshot is None or snapshot["user_id"] != uid:
                fail(404, "not_found", "Statement snapshot not found")
            return self._statement_page(snapshot["result"], limit, offset)

        from_value = timestamp_param(query, "from")
        to_value = timestamp_param(query, "to")
        known_value = timestamp_param(query, "known_at")
        lower = from_value[1] if from_value else None
        upper_raw = to_value[0] if to_value else read_at.isoformat()
        upper = parse_timestamp(upper_raw)
        known = min(known_value[1], read_at) if known_value else read_at
        known_filter = known.isoformat()
        if lower is not None and lower > upper:
            fail(422, "validation_failed", "from must be earlier than to")

        selected = []
        for payment in self.state["payments"]:
            if uid not in (payment["from_user_id"], payment["to_user_id"]):
                continue
            revision = self._selected_revision(payment, known_filter)
            if revision is None:
                continue
            effective = parse_timestamp(revision["effective_at"])
            if ((lower is not None and effective < lower) or effective >= upper):
                continue
            payment_obj = self._payment_obj(payment)
            payment_obj["amount"] = revision["amount"]
            delta = revision["amount"] * (
                1 if payment["to_user_id"] == uid else -1)
            selected.append((effective, payment["id"], payment_obj, delta, revision))
        selected.sort(key=lambda item: (item[0], item[1]))

        opening = (self._opening_balance(uid) if lower is None else
                   self._historical_total(uid, lower, known_filter,
                                          inclusive=False))
        closing = self._historical_total(uid, upper, known_filter,
                                         inclusive=False)
        running = opening
        entries = []
        for effective, payment_id, payment_obj, delta, revision in selected:
            running += delta
            entries.append({"payment": payment_obj, "delta": delta,
                            "balance_after": running, "revision": revision["revision"],
                            "effective_at": revision["effective_at"],
                            "recorded_at": revision["recorded_at"]})
        result = {"opening_balance": opening, "entries": entries,
                  "closing_balance": closing, "has_more": False}
        if known_value:
            result["known_at"] = known_value[0]
        if from_value:
            result["from"] = from_value[0]
        result["to"] = upper_raw
        snapshot_token = secrets.token_urlsafe(32)
        result["snapshot"] = snapshot_token
        self.statement_snapshots[snapshot_token] = {
            "user_id": uid, "result": copy.deepcopy(result)}
        return self._statement_page(result, limit, offset)

    def _statement_page(self, source, limit, offset):
        result = copy.deepcopy(source)
        entries = result["entries"]
        result["entries"] = entries[offset:offset + limit]
        result["has_more"] = offset + limit < len(entries)
        return result

    def _authorization_remaining(self, item):
        return item["amount"] - item.get("captured_amount", 0)

    def _held(self, uid):
        return sum(self._authorization_remaining(item) for item in self.state["authorizations"]
                   if item["from_user_id"] == uid and item["status"] == "open")

    def _available(self, uid):
        return self.state["users"][uid]["balance"] - self._held(uid)

    def _expire_authorizations(self, at=None):
        instant = at or datetime.now(timezone.utc)
        for item in self.state["authorizations"]:
            if (item["status"] == "open" and
                    (parse_timestamp(item["expires_at"]) or instant) <= instant):
                item["status"] = "expired"
                item["closed_at"] = item["expires_at"]

    def _authorization_obj(self, item):
        return {"authorization_id": item["id"],
                "from_user_id": item["from_user_id"],
                "from_handle": self.state["users"][item["from_user_id"]]["handle"],
                "to_user_id": item["to_user_id"],
                "to_handle": self.state["users"][item["to_user_id"]]["handle"],
                "amount": item["amount"],
                "captured_amount": item.get("captured_amount", 0),
                "currency": self.state["currency"], "note": item["note"],
                "visibility": item["visibility"], "status": item["status"],
                "expires_at": item["expires_at"],
                "payment_id": item.get("payment_id"),
                "payment_ids": list(item.get("payment_ids", [])),
                "remaining_amount": (self._authorization_remaining(item)
                                      if item["status"] == "open" else 0),
                "created_at": item["created_at"],
                "closed_at": item.get("closed_at")}

    def _make_request(self, requester_id, payer_id, amount, note, created=None, rid=None):
        item = {"id": rid or new_id("rq"), "requester_id": requester_id,
                "payer_id": payer_id, "amount": amount, "note": note,
                "status": "pending", "payment_id": None, "created_at": created or now()}
        self.state["requests"].append(item)
        return item

    def _make_payment(self, from_id, to_id, amount, note, vis, request_id=None,
                      settlement_id=None, authorization_id=None, created=None,
                      reserved_authorization=None, refund_of=None):
        sender, receiver = self.state["users"][from_id], self.state["users"][to_id]
        if reserved_authorization is None and self._available(from_id) < amount:
            fail(409, "insufficient_funds", "Insufficient funds")
        if sender["balance"] < amount:
            fail(409, "insufficient_funds", "Insufficient funds")
        if receiver["balance"] + amount > MAX_BALANCE:
            fail(422, "validation_failed", "Resulting balance exceeds the supported range")
        # Check both sides before either write so a rejected transfer is mutation-free.
        sender["balance"] -= amount
        receiver["balance"] += amount
        item = {"id": new_id("p"), "from_user_id": from_id, "to_user_id": to_id,
                "amount": amount, "note": note, "visibility": vis,
                "request_id": request_id, "settlement_id": settlement_id,
                "authorization_id": authorization_id, "refund_of": refund_of,
                "created_at": created or now()}
        return self._append_payment(item)

    def _key_and_replay(self, headers, uid, method, path, body):
        key = headers.get("Idempotency-Key", "")
        if not key:
            fail(400, "missing_idempotency_key", "Idempotency-Key is required")
        if len(key) > 255:
            fail(422, "validation_failed", "Idempotency-Key exceeds 255 characters")
        slot = self._idempotency_slot(uid, key, method, path)
        prior = self.state["idempotency"].get(slot)
        request_body = canonical(body)
        if prior is not None:
            if prior["method"] != method or prior["path"] != path or prior["body"] != request_body:
                fail(409, "idempotency_key_reuse", "Idempotency-Key was already used for another request")
            return 200, copy.deepcopy(prior["response"]), None
        return None, None, (slot, method, path, request_body)

    def _save_idempotency(self, signature, response):
        slot, method, path, request_body = signature
        self.state["idempotency"][slot] = {"method": method, "path": path,
                                            "body": request_body,
                                            "response": copy.deepcopy(response)}

    def _create_payment(self, uid, body, signature):
        amount = amount_value(body.get("amount"))
        handle = required_string(body, "to_handle")
        note, vis = optional_note(body), visibility(body)
        if handle == self.state["users"][uid]["handle"]:
            fail(422, "self_payment", "A wallet cannot pay itself")
        receiver = self._resolve(handle)
        payment = self._make_payment(uid, receiver["id"], amount, note, vis)
        response = self._payment_obj(payment)
        self._save_idempotency(signature, response)
        return 201, response

    def _create_authorization(self, uid, body, signature):
        amount = amount_value(body.get("amount"))
        handle = required_string(body, "to_handle")
        note, vis = optional_note(body), visibility(body)
        if handle == self.state["users"][uid]["handle"]:
            fail(422, "self_payment", "A wallet cannot authorize itself")
        receiver = self._resolve(handle)
        if self._available(uid) < amount:
            fail(409, "insufficient_funds", "Insufficient available funds")
        created_dt = datetime.now(timezone.utc)
        try:
            expires_dt = created_dt + timedelta(seconds=self.state["authorization_ttl_seconds"])
        except OverflowError:
            fail(422, "validation_failed", "Authorization lifetime is outside the supported range")
        item = {"id": new_id("a"), "from_user_id": uid, "to_user_id": receiver["id"],
                "amount": amount, "captured_amount": 0, "note": note,
                "visibility": vis, "status": "open", "expires_at": expires_dt.isoformat(),
                "payment_id": None, "payment_ids": [], "created_at": created_dt.isoformat(),
                "closed_at": None, "capture_events": []}
        self.state["authorizations"].append(item)
        response = self._authorization_obj(item)
        self._save_idempotency(signature, response)
        return 201, response

    def _capture_authorization(self, uid, authorization_id, body, signature):
        item = next((a for a in self.state["authorizations"] if a["id"] == authorization_id), None)
        if item is None:
            fail(404, "not_found", "Authorization not found")
        if item["to_user_id"] != uid:
            fail(403, "forbidden", "Only the recipient may capture this authorization")
        if item["status"] == "expired" or (parse_timestamp(item["expires_at"]) or datetime.min.replace(
                tzinfo=timezone.utc)) <= datetime.now(timezone.utc):
            item["status"] = "expired"
            fail(409, "authorization_expired", "Authorization has expired")
        if item["status"] != "open":
            fail(409, "authorization_not_open", "Authorization is not open")
        remaining = self._authorization_remaining(item)
        if "amount" in body:
            requested = body["amount"]
            # Establish that the supplied amount is a positive integer first.
            # Only well-formed minor-unit amounts get the authorization-specific
            # over-capture error; e.g. 2000.5 is a malformed amount even when it
            # is also greater than a 2000-unit remainder.
            numeric = None
            if isinstance(requested, int) and not isinstance(requested, bool):
                numeric = requested
            elif isinstance(requested, float):
                if math.isfinite(requested) and requested.is_integer():
                    numeric = int(requested)
            elif isinstance(requested, Decimal):
                if requested.is_finite() and requested == requested.to_integral_value():
                    numeric = requested
            elif isinstance(requested, RawJsonNumber):
                try:
                    parsed = Decimal(requested.token)
                    if parsed.is_finite() and parsed == parsed.to_integral_value():
                        numeric = parsed
                except (InvalidOperation, ValueError):
                    pass
            if numeric is None or numeric < 1:
                fail(422, "validation_failed", "amount must be a positive integer minor-unit value")
            if numeric > remaining:
                fail(422, "capture_exceeds_authorization", "Capture exceeds the remaining authorization")
            amount = int(numeric)
        else:
            amount = remaining
        if amount > remaining:
            fail(422, "capture_exceeds_authorization", "Capture exceeds the remaining authorization")
        final = body.get("final", True)
        if not isinstance(final, bool):
            fail(400, "malformed_request", "final must be a boolean")
        payment = self._make_payment(
            item["from_user_id"], item["to_user_id"], amount, item["note"], item["visibility"],
            authorization_id=item["id"], reserved_authorization=item)
        item["captured_amount"] = item.get("captured_amount", 0) + amount
        item.setdefault("payment_ids", []).append(payment["id"])
        item["payment_id"] = payment["id"]
        closed = final or self._authorization_remaining(item) == 0
        item.setdefault("capture_events", []).append(
            {"payment_id": payment["id"], "final": closed})
        if closed:
            item["status"] = "captured"
            item["closed_at"] = payment["created_at"]
        response = self._payment_obj(payment)
        self._save_idempotency(signature, response)
        return 201, response

    def _void_authorization(self, uid, authorization_id):
        item = next((a for a in self.state["authorizations"] if a["id"] == authorization_id), None)
        if item is None:
            fail(404, "not_found", "Authorization not found")
        if item["from_user_id"] != uid:
            fail(403, "forbidden", "Only the payer may void this authorization")
        if item["status"] == "voided":
            return 200, self._authorization_obj(item)
        if item["status"] != "open":
            fail(409, "authorization_not_open", "Authorization is not open")
        item["status"] = "voided"
        item["closed_at"] = now()
        return 200, self._authorization_obj(item)

    def _create_request(self, uid, body, signature):
        amount = amount_value(body.get("amount"))
        handle = required_string(body, "payer_handle")
        note = optional_note(body)
        if handle == self.state["users"][uid]["handle"]:
            fail(422, "self_request", "A user cannot request money from themselves")
        payer = self._resolve(handle)
        item = self._make_request(uid, payer["id"], amount, note)
        response = self._request_obj(item)
        self._save_idempotency(signature, response)
        return 201, response

    def _pay_request(self, uid, rid, body, signature):
        vis = visibility(body)
        item = next((r for r in self.state["requests"] if r["id"] == rid), None)
        if item is None:
            fail(404, "not_found", "Request not found")
        if item["payer_id"] != uid:
            fail(403, "forbidden", "Only the payer may pay this request")
        if item["status"] != "pending":
            fail(409, "request_not_pending", "Request is not pending")
        payment = self._make_payment(uid, item["requester_id"], item["amount"], item["note"], vis,
                                     request_id=rid)
        item["status"], item["payment_id"] = "paid", payment["id"]
        response = self._payment_obj(payment)
        self._save_idempotency(signature, response)
        return 201, response

    def _create_split(self, uid, body, signature):
        amount = amount_value(body.get("amount"))
        raw_handles = body.get("participant_handles")
        if not isinstance(raw_handles, list) or not raw_handles:
            fail(422, "validation_failed", "participant_handles must be a non-empty array")
        if any(not isinstance(h, str) for h in raw_handles):
            fail(422, "validation_failed", "participant handles must be strings")
        if len(set(raw_handles)) != len(raw_handles):
            fail(422, "validation_failed", "participant handles must be unique")
        note = optional_note(body)
        people = [self._resolve(h) for h in raw_handles]
        quotient, remainder = divmod(amount, len(people))
        shares = [{"handle": person["handle"], "amount": quotient + (1 if i < remainder else 0)}
                  for i, person in enumerate(people)]
        ridx = uid
        requests = []
        for person, share in zip(people, shares):
            if person["id"] != ridx:
                requests.append(self._request_obj(self._make_request(uid, person["id"],
                                                                      share["amount"], note)))
        response = {"split_id": new_id("sp"), "amount": amount,
                    "currency": self.state["currency"], "note": note,
                    "shares": shares, "requests": requests, "created_at": now()}
        self._save_idempotency(signature, response)
        return 201, response

    def _settle(self, uid, body, signature):
        if uid not in self.state["operators"]:
            fail(403, "forbidden", "Settlement operator permission required")
        transfers = body.get("transfers")
        if not isinstance(transfers, list) or not 1 <= len(transfers) <= 32:
            fail(422, "validation_failed", "transfers must contain 1 to 32 entries")
        entries = []
        for transfer in transfers:
            if not isinstance(transfer, dict):
                fail(422, "validation_failed", "Each transfer must be an object")
            amount = amount_value(transfer.get("amount"))
            if "from_handle" not in transfer or "to_handle" not in transfer:
                fail(422, "validation_failed", "Each transfer requires from_handle and to_handle")
            from_handle, to_handle = transfer["from_handle"], transfer["to_handle"]
            if not isinstance(from_handle, str) or not isinstance(to_handle, str):
                fail(400, "malformed_request", "Transfer handles must be strings")
            note, vis = optional_note(transfer), visibility(transfer)
            if from_handle == to_handle:
                fail(422, "self_payment", "A wallet cannot pay itself")
            sender, receiver = self._resolve(from_handle), self._resolve(to_handle)
            entries.append((sender["id"], receiver["id"], amount, note, vis))
        net = {u["id"]: 0 for u in self.state["users"].values()}
        for sender, receiver, amount, _, _ in entries:
            net[sender] -= amount
            net[receiver] += amount
        totals = {u["id"]: u["balance"] + net[u["id"]]
                  for u in self.state["users"].values()}
        if any(totals[uid] - self._held(uid) < 0 for uid in totals):
            fail(409, "insufficient_funds", "Settlement is not affordable")
        if any(v < 0 for v in totals.values()):
            fail(409, "insufficient_funds", "Settlement is not affordable")
        if any(v > MAX_BALANCE for v in totals.values()):
            fail(422, "validation_failed", "Settlement balance exceeds the supported range")
        settlement_id, committed_at = new_id("st"), now()
        payments = []
        for sender, receiver, amount, note, vis in entries:
            item = {"id": new_id("p"), "from_user_id": sender, "to_user_id": receiver,
                    "amount": amount, "note": note, "visibility": vis,
                    "request_id": None, "settlement_id": settlement_id,
                    "authorization_id": None, "refund_of": None,
                    "created_at": committed_at}
            item["revisions"] = [self._initial_revision(amount, committed_at)]
            payments.append(item)
        # Commit only after every entry and every net balance has passed validation.
        for user in self.state["users"].values():
            user["balance"] = totals[user["id"]]
        self.state["payments"].extend(payments)
        response = {"settlement_id": settlement_id, "committed_at": committed_at,
                    "payments": [self._payment_obj(p) for p in payments]}
        self._save_idempotency(signature, response)
        return 201, response

    def _serialize_state(self):
        state = copy.deepcopy(self.state)
        state.pop("tokens", None)
        return state

    def _validate_import(self, document):
        version = document.get("format_version") if isinstance(document, dict) else None
        if (not isinstance(document, dict) or document.get("track") != "pocketful"
                or not isinstance(version, int) or isinstance(version, bool) or version != 1):
            fail(422, "validation_failed", "Invalid export document")
        source_state = document.get("state")
        if not isinstance(source_state, dict):
            fail(422, "validation_failed", "Invalid export state")
        legacy_keys = {"currency", "minor_units", "users", "emails", "handles", "tokens",
                       "payments", "requests", "operators", "idempotency", "seeded_total", "sequence"}
        tokenless_legacy_keys = legacy_keys - {"tokens"}
        stage2_keys = legacy_keys | {"authorization_ttl_seconds", "authorizations"}
        tokenless_stage2_keys = stage2_keys - {"tokens"}
        stage3_keys = stage2_keys | {"opening_balances"}
        tokenless_stage3_keys = stage3_keys - {"tokens"}
        legacy_stage3_keys = legacy_keys | {"opening_balances"}
        tokenless_legacy_stage3_keys = legacy_stage3_keys - {"tokens"}
        if set(source_state) not in (legacy_keys, tokenless_legacy_keys, stage2_keys,
                                     tokenless_stage2_keys, stage3_keys, tokenless_stage3_keys,
                                     legacy_stage3_keys, tokenless_legacy_stage3_keys):
            fail(422, "validation_failed", "Invalid export state")
        state = copy.deepcopy(source_state)
        state.setdefault("tokens", {})
        if "authorization_ttl_seconds" not in source_state:
            state["authorization_ttl_seconds"] = 600
        if "authorizations" not in source_state:
            state["authorizations"] = []
        stage3_export = "opening_balances" in source_state
        # Validate referential and arithmetic integrity while preserving byte-for-byte values.
        try:
            if not isinstance(state["users"], dict) or not isinstance(state["payments"], list) or not isinstance(state["requests"], list):
                raise ValueError()
            if (not isinstance(state["tokens"], dict) or not isinstance(state["idempotency"], dict)
                    or not isinstance(state["emails"], dict) or not isinstance(state["handles"], dict)):
                raise ValueError()
            ttl = state["authorization_ttl_seconds"]
            if not isinstance(ttl, int) or isinstance(ttl, bool) or ttl < 1:
                raise ValueError()
            if not isinstance(state["authorizations"], list):
                raise ValueError()
            if (not isinstance(state["minor_units"], int) or isinstance(state["minor_units"], bool)
                    or (state["currency"], state["minor_units"]) not in (("EUR", 2), ("JPY", 0), ("BHD", 3))):
                raise ValueError()
            balances = 0
            for uid, user in state["users"].items():
                if (not isinstance(uid, str) or not uid or len(uid) > 64 or not isinstance(user, dict)
                        or user.get("id") != uid or not isinstance(user.get("email"), str)
                        or not isinstance(user.get("display_name"), str)
                        or not isinstance(user.get("handle"), str)
                        or not HANDLE_RE.fullmatch(user["handle"])):
                    raise ValueError()
                if not isinstance(user.get("balance"), int) or isinstance(user["balance"], bool) or not 0 <= user["balance"] <= MAX_BALANCE:
                    raise ValueError()
                if (not isinstance(user.get("password_hash"), str)
                        or not PASSWORD_HASH_RE.fullmatch(user["password_hash"])):
                    raise ValueError()
                balances += user["balance"]
            if len(state["emails"]) != len(state["users"]) or len(state["handles"]) != len(state["users"]):
                raise ValueError()
            for email, uid in state["emails"].items():
                if uid not in state["users"] or state["users"][uid].get("email") != email:
                    raise ValueError()
            for handle, uid in state["handles"].items():
                if uid not in state["users"] or state["users"][uid].get("handle") != handle:
                    raise ValueError()
            for token, uid in state["tokens"].items():
                if not token or uid not in state["users"]:
                    raise ValueError()
            payment_ids = set()
            payment_by_id = {}
            for p in state["payments"]:
                if (not isinstance(p, dict) or not isinstance(p.get("id"), str) or not p["id"]
                        or len(p["id"]) > 64 or p["id"] in payment_ids
                        or not isinstance(p.get("from_user_id"), str)
                        or not isinstance(p.get("to_user_id"), str)
                        or p["from_user_id"] not in state["users"]
                        or p["to_user_id"] not in state["users"]
                        or p["from_user_id"] == p["to_user_id"]
                        or not isinstance(p.get("amount"), int) or isinstance(p["amount"], bool)
                        or not 0 <= p["amount"] <= MAX_AMOUNT
                        or (p["amount"] == 0 and not isinstance(p.get("request_id"), str))
                        or not isinstance(p.get("note"), str)
                        or len(p["note"]) > 200 or p.get("visibility") not in ("public", "private")
                        or parse_timestamp(p.get("created_at")) is None):
                    raise ValueError()
                revisions = p.get("revisions")
                if revisions is None:
                    revisions = [self._initial_revision(p["amount"], p["created_at"])]
                    p["revisions"] = revisions
                if not isinstance(revisions, list) or not revisions:
                    raise ValueError()
                previous_recorded = None
                for index, revision in enumerate(revisions, 1):
                    if (not isinstance(revision, dict)
                            or revision.get("revision") != index
                            or isinstance(revision.get("revision"), bool)
                            or not isinstance(revision.get("amount"), int)
                            or isinstance(revision.get("amount"), bool)
                            or not 0 <= revision["amount"] <= MAX_AMOUNT
                            or parse_timestamp(revision.get("effective_at")) is None
                            or parse_timestamp(revision.get("recorded_at")) is None
                            or not isinstance(revision.get("reason"), str)
                            or len(revision["reason"]) > 200):
                        raise ValueError()
                    batch_id = revision.get("correction_batch_id")
                    if batch_id is not None and (index == 1 or not isinstance(batch_id, str)
                                                 or not batch_id or len(batch_id) > 64):
                        raise ValueError()
                    if index == 1:
                        if (revision["amount"] != p["amount"]
                                or revision["effective_at"] != p["created_at"]
                                or revision["recorded_at"] != p["created_at"]
                                or revision["reason"] != ""):
                            raise ValueError()
                    elif (len(revision["reason"]) < 1
                          or parse_timestamp(revision["recorded_at"]) <= previous_recorded
                          or parse_timestamp(revision["effective_at"])
                             > parse_timestamp(revision["recorded_at"])):
                        raise ValueError()
                    previous_recorded = parse_timestamp(revision["recorded_at"])
                # Settlement members may carry batch revisions; captures and refunds never change.
                if ((p.get("authorization_id") is not None or p.get("refund_of") is not None)
                        and len(revisions) != 1):
                    raise ValueError()
                payment_ids.add(p["id"])
                payment_by_id[p["id"]] = p
            validate_refund_links(state["payments"])
            request_ids = set()
            for r in state["requests"]:
                if (not isinstance(r, dict) or not isinstance(r.get("id"), str) or not r["id"]
                        or len(r["id"]) > 64 or r["id"] in request_ids
                        or not isinstance(r.get("requester_id"), str)
                        or not isinstance(r.get("payer_id"), str)
                        or r["requester_id"] not in state["users"]
                        or r["payer_id"] not in state["users"]
                        or r["requester_id"] == r["payer_id"]
                        or not isinstance(r.get("amount"), int) or isinstance(r["amount"], bool)
                        or not 0 <= r["amount"] <= MAX_AMOUNT
                        or not isinstance(r.get("status"), str) or r["status"] not in STATUSES
                        or not isinstance(r.get("note"), str) or len(r["note"]) > 200
                        or parse_timestamp(r.get("created_at")) is None):
                    raise ValueError()
                request_ids.add(r["id"])
            validate_financial_links(state["payments"], state["requests"])
            authorization_ids = set()
            authorization_by_id = {}
            held_by_user = {uid: 0 for uid in state["users"]}
            for a in state["authorizations"]:
                if (not isinstance(a, dict) or not isinstance(a.get("id"), str) or not a["id"]
                        or len(a["id"]) > 64 or a["id"] in authorization_ids
                        or not isinstance(a.get("from_user_id"), str)
                        or not isinstance(a.get("to_user_id"), str)
                        or a["from_user_id"] not in state["users"]
                        or a["to_user_id"] not in state["users"]
                        or a["from_user_id"] == a["to_user_id"]
                        or not isinstance(a.get("amount"), int) or isinstance(a["amount"], bool)
                        or not 1 <= a["amount"] <= MAX_AMOUNT
                        or not isinstance(a.get("captured_amount", 0), int)
                        or isinstance(a.get("captured_amount", 0), bool)
                        or not 0 <= a.get("captured_amount", 0) <= a["amount"]
                        or a.get("status") not in ("open", "captured", "voided", "expired")
                        or not isinstance(a.get("note", ""), str)
                        or len(a.get("note", "")) > 200
                        or a.get("visibility", "public") not in ("public", "private")
                        or not isinstance(a.get("visibility", "public"), str)
                        or parse_timestamp(a.get("expires_at")) is None
                        or ("created_at" in a and parse_timestamp(a.get("created_at")) is None)):
                    raise ValueError()
                authorization_ids.add(a["id"])
                authorization_by_id[a["id"]] = a
                a.setdefault("captured_amount", 0)
                a.setdefault("note", "")
                a.setdefault("visibility", "public")
                a.setdefault("created_at", now())
                a.setdefault("payment_ids", [a["payment_id"]] if a.get("payment_id") else [])
                a.setdefault("payment_id", a["payment_ids"][-1] if a["payment_ids"] else None)
                if (not isinstance(a["payment_ids"], list)
                        or any(not isinstance(pid, str) or not pid for pid in a["payment_ids"])
                        or len(set(a["payment_ids"])) != len(a["payment_ids"])
                        or (a["payment_id"] is not None and not isinstance(a["payment_id"], str))
                        or (a["payment_ids"] and a["payment_id"] != a["payment_ids"][-1])
                        or (not a["payment_ids"] and a["payment_id"] is not None)):
                    raise ValueError()
                for pid in a["payment_ids"]:
                    payment = payment_by_id.get(pid)
                    if (payment is None or payment.get("authorization_id") != a["id"]
                            or payment["from_user_id"] != a["from_user_id"]
                            or payment["to_user_id"] != a["to_user_id"]):
                        raise ValueError()
                linked_payments = [p for p in state["payments"]
                                   if p.get("authorization_id") == a["id"]]
                if linked_payments:
                    if ([p["id"] for p in linked_payments] != a["payment_ids"]
                            or sum(p["amount"] for p in linked_payments) != a["captured_amount"]):
                        raise ValueError()
                elif a["payment_ids"]:
                    raise ValueError()
                if "closed_at" not in a:
                    if a["status"] == "expired":
                        a["closed_at"] = a["expires_at"]
                    elif a["status"] == "captured" and linked_payments:
                        a["closed_at"] = linked_payments[-1]["created_at"]
                    elif a["status"] in ("captured", "voided"):
                        # Older exports did not record when a hold closed. A closed hold
                        # need not reconstruct its lifecycle, so it releases at creation
                        # rather than appearing held until the import.
                        a["closed_at"] = a["created_at"]
                    else:
                        a["closed_at"] = None
                if (a["closed_at"] is not None
                        and parse_timestamp(a["closed_at"]) is None):
                    raise ValueError()
                if a["status"] == "open" and a["closed_at"] is not None:
                    raise ValueError()
                expected_events = [{"payment_id": p["id"],
                    "final": a["status"] == "captured" and p["id"] == a["payment_id"]}
                    for p in linked_payments]
                if "capture_events" not in a:
                    a["capture_events"] = expected_events
                if (not isinstance(a["capture_events"], list)
                        or a["capture_events"] != expected_events):
                    raise ValueError()
                if a["status"] == "open":
                    if a["captured_amount"] >= a["amount"]:
                        raise ValueError()
                    expiry = parse_timestamp(a["expires_at"])
                    if expiry <= datetime.now(timezone.utc):
                        a["status"] = "expired"
                        a["closed_at"] = a["expires_at"]
                    else:
                        held_by_user[a["from_user_id"]] += a["amount"] - a["captured_amount"]
            if any(held_by_user[uid] > state["users"][uid]["balance"] for uid in held_by_user):
                raise ValueError()
            for p in state["payments"]:
                aid = p.get("authorization_id")
                if aid is not None:
                    a = authorization_by_id.get(aid)
                    if (a is None or p.get("request_id") is not None
                            or p.get("settlement_id") is not None or p["id"] not in a["payment_ids"]):
                        raise ValueError()
            if not isinstance(state["operators"], list) or any(uid not in state["users"] for uid in state["operators"]):
                raise ValueError()
            payments_by_id = {p["id"]: p for p in state["payments"]}
            requests_by_id = {r["id"]: r for r in state["requests"]}
            for slot, entry in state["idempotency"].items():
                if (not isinstance(slot, str) or not isinstance(entry, dict)
                        or set(entry) != {"method", "path", "body", "response"}):
                    raise ValueError()
                method, path = entry["method"], entry["path"]
                if (method != "POST" or not isinstance(path, str)
                        or not isinstance(entry["body"], str)
                        or not isinstance(entry["response"], dict)):
                    raise ValueError()
                pay_match = re.fullmatch(r"/requests/([^/]+)/pay", path)
                capture_match = re.fullmatch(r"/authorizations/([^/]+)/capture", path)
                correction_match = re.fullmatch(r"/payments/([^/]+)/corrections", path)
                refund_match = re.fullmatch(r"/payments/([^/]+)/refunds", path)
                assistant_confirmation = re.fullmatch(r"/ai/proposals/[^/]+/confirm", path)
                if (path not in ("/payments", "/requests", "/splits", "/settlements",
                                 "/authorizations", "/correction-batches")
                        and not pay_match and not capture_match
                        and not correction_match and not refund_match
                        and not assistant_confirmation):
                    raise ValueError()
                suffix = "\0" + method + "\0" + path
                if not slot.endswith(suffix):
                    raise ValueError()
                prefix = slot[:-len(suffix)]
                if not any(prefix.startswith(uid + "\0")
                           and 1 <= len(prefix[len(uid) + 1:]) <= 255
                           for uid in state["users"]):
                    raise ValueError()
                response = entry["response"]
                if path == "/payments":
                    payment = payments_by_id.get(response.get("payment_id"))
                    if payment is None or payment.get("request_id") is not None or payment.get("settlement_id") is not None:
                        raise ValueError()
                elif path == "/requests":
                    if response.get("request_id") not in requests_by_id:
                        raise ValueError()
                elif pay_match:
                    payment = payments_by_id.get(response.get("payment_id"))
                    if (payment is None or payment.get("request_id") != pay_match.group(1)
                            or response.get("request_id") != pay_match.group(1)):
                        raise ValueError()
                elif path == "/authorizations":
                    if response.get("authorization_id") not in authorization_by_id:
                        raise ValueError()
                elif capture_match:
                    payment = payments_by_id.get(response.get("payment_id"))
                    if (payment is None or payment.get("authorization_id") != capture_match.group(1)
                            or response.get("authorization_id") != capture_match.group(1)):
                        raise ValueError()
                elif correction_match:
                    payment = payments_by_id.get(response.get("payment_id"))
                    if (payment is None or payment["id"] != correction_match.group(1)
                            or not isinstance(response.get("revision"), int)
                            or isinstance(response.get("revision"), bool)
                            or not 2 <= response["revision"] <= len(payment["revisions"])
                            or payment["revisions"][response["revision"] - 1]["amount"]
                               != response.get("amount")):
                        raise ValueError()
                elif refund_match:
                    payment = payments_by_id.get(response.get("payment_id"))
                    if payment is None or payment.get("refund_of") != refund_match.group(1):
                        raise ValueError()
                elif path == "/correction-batches":
                    batch_id = response.get("correction_batch_id")
                    revisions = response.get("revisions")
                    if (not isinstance(batch_id, str) or not batch_id
                            or not isinstance(revisions, list) or not revisions):
                        raise ValueError()
                    for item in revisions:
                        payment = payments_by_id.get(item.get("payment_id") if isinstance(item, dict) else None)
                        number = item.get("revision") if payment is not None else None
                        if (not isinstance(number, int) or isinstance(number, bool)
                                or not 2 <= number <= len(payment["revisions"])
                                or payment["revisions"][number - 1].get("correction_batch_id") != batch_id):
                            raise ValueError()
                elif path == "/splits":
                    if (not isinstance(response.get("split_id"), str) or not response["split_id"]
                            or not isinstance(response.get("requests"), list)
                            or any(not isinstance(r, dict) or r.get("request_id") not in requests_by_id
                                   for r in response["requests"])):
                        raise ValueError()
                elif assistant_confirmation:
                    if (response.get("confirmed") is not True
                            or response.get("action") not in ("split", "request", "paypal_checkout")
                            or not isinstance(response.get("result"), dict)):
                        raise ValueError()
                else:
                    settlement_id = response.get("settlement_id")
                    members = response.get("payments")
                    if (not isinstance(settlement_id, str) or not settlement_id
                            or not isinstance(members, list) or not members):
                        raise ValueError()
                    for member in members:
                        payment = payments_by_id.get(member.get("payment_id") if isinstance(member, dict) else None)
                        if payment is None or payment.get("settlement_id") != settlement_id:
                            raise ValueError()
            if (not isinstance(state["sequence"], int) or isinstance(state["sequence"], bool)
                    or state["sequence"] < 0
                    or not isinstance(state["seeded_total"], int) or isinstance(state["seeded_total"], bool)
                    or state["seeded_total"] != balances):
                raise ValueError()
            if stage3_export:
                openings = state["opening_balances"]
                if (not isinstance(openings, dict) or set(openings) != set(state["users"])
                        or any(not isinstance(value, int) or isinstance(value, bool) or value < 0
                               for value in openings.values())):
                    raise ValueError()
            else:
                net = {uid: 0 for uid in state["users"]}
                for payment in state["payments"]:
                    net[payment["from_user_id"]] -= payment["amount"]
                    net[payment["to_user_id"]] += payment["amount"]
                state["opening_balances"] = {
                    uid: user["balance"] - net[uid]
                    for uid, user in state["users"].items()}
                if any(value < 0 for value in state["opening_balances"].values()):
                    raise ValueError()
        except (KeyError, TypeError, ValueError):
            fail(422, "validation_failed", "Invalid export state")
        prior_state = self.state
        self.state = state
        try:
            current_at = datetime.now(timezone.utc)
            if any(self._historical_total(uid, current_at) != user["balance"]
                   for uid, user in state["users"].items()):
                fail(422, "validation_failed", "Export balances do not match payment history")
            # Earlier stages did not record full hold lifecycles, so their exports are
            # checked for historical totals only; reconstructed holds must not reject them.
            if not self._record_historical_state_is_solvent(include_holds=stage3_export):
                fail(422, "validation_failed", "Export contains a historical overdraft")
        finally:
            self.state = prior_state
        return copy.deepcopy(state)

    def _validate_snapshots(self, document, state):
        """Statement snapshots travel beside the state; exports from earlier stages have none.

        An import replaces every existing snapshot, so tokens from the replaced state stop
        resolving and tokens from the imported service keep paging their frozen result.
        """
        raw = document.get("statement_snapshots", {})
        if not isinstance(raw, dict):
            fail(422, "validation_failed", "Invalid statement snapshots")
        snapshots = {}
        for token, snapshot in raw.items():
            result = snapshot.get("result") if isinstance(snapshot, dict) else None
            if (not isinstance(token, str) or not token or len(token) > 255
                    or not isinstance(snapshot, dict)
                    or set(snapshot) != {"user_id", "result"}
                    or not isinstance(snapshot["user_id"], str)
                    or snapshot["user_id"] not in state["users"]
                    or not isinstance(result, dict) or result.get("snapshot") != token
                    or not isinstance(result.get("entries"), list)
                    or any(not isinstance(result.get(name), int) or isinstance(result.get(name), bool)
                           for name in ("opening_balance", "closing_balance"))):
                fail(422, "validation_failed", "Invalid statement snapshots")
            snapshots[token] = copy.deepcopy(snapshot)
        return snapshots

    def _reset(self, fixture):
        if not isinstance(fixture, dict):
            fail(422, "validation_failed", "Fixture must be an object")
        temp = empty_state()
        reset_at = now()
        reset_instant = parse_timestamp(reset_at)
        currency = fixture.get("currency")
        minor = integer_in_range(fixture.get("minor_units"), 0, 3)
        if not isinstance(currency, str) or minor not in (0, 2, 3):
            fail(422, "validation_failed", "Invalid currency configuration")
        if (currency, minor) not in (("EUR", 2), ("JPY", 0), ("BHD", 3)):
            fail(422, "validation_failed", "Unsupported currency configuration")
        temp["currency"], temp["minor_units"] = currency, minor
        ttl = fixture.get("authorization_ttl_seconds", 600)
        if isinstance(ttl, bool) or not isinstance(ttl, (int, Decimal)):
            fail(422, "validation_failed", "authorization_ttl_seconds must be a positive integer")
        if isinstance(ttl, Decimal):
            if not ttl.is_finite() or ttl != ttl.to_integral_value():
                fail(422, "validation_failed", "authorization_ttl_seconds must be a positive integer")
            ttl = int(ttl)
        if ttl < 1:
            fail(422, "validation_failed", "authorization_ttl_seconds must be a positive integer")
        temp["authorization_ttl_seconds"] = ttl
        users = fixture.get("users", [])
        if not isinstance(users, list):
            fail(422, "validation_failed", "users must be an array")
        pending_user_hashes = []
        for raw in users:
            if not isinstance(raw, dict):
                fail(422, "validation_failed", "Invalid user fixture")
            uid, email, password = raw.get("id"), raw.get("email"), raw.get("password")
            display, handle = raw.get("display_name"), raw.get("handle")
            balance = integer_in_range(raw.get("balance"), 0, MAX_BALANCE)
            if (not isinstance(uid, str) or not uid or len(uid) > 64 or
                not isinstance(email, str) or not isinstance(password, str) or
                not isinstance(display, str) or not isinstance(handle, str) or
                not HANDLE_RE.fullmatch(handle) or balance is None or uid in temp["users"] or
                email in temp["emails"] or handle in temp["handles"]):
                fail(422, "validation_failed", "Invalid or duplicate user fixture")
            user = {"id": uid, "email": email, "password_hash": "",
                    "display_name": display, "handle": handle, "balance": balance}
            temp["users"][uid], temp["emails"][email], temp["handles"][handle] = user, uid, uid
            pending_user_hashes.append((uid, password))
        if pending_user_hashes:
            # Fixture reset is test-only and can contain dozens of users. Hash distinct
            # credentials concurrently so large invariant fixtures fit the HTTP timeout.
            with ThreadPoolExecutor(max_workers=min(4, len(pending_user_hashes))) as pool:
                hashes = pool.map(password_hash, (password for _, password in pending_user_hashes))
                for (uid, _), hashed in zip(pending_user_hashes, hashes):
                    temp["users"][uid]["password_hash"] = hashed
        temp["seeded_total"] = sum(user["balance"] for user in temp["users"].values())
        payments = fixture.get("payments", [])
        if not isinstance(payments, list):
            fail(422, "validation_failed", "payments must be an array")
        payment_ids = set()
        for raw in payments:
            if not isinstance(raw, dict):
                fail(422, "validation_failed", "Invalid payment fixture")
            pid, sender, receiver = raw.get("id"), raw.get("from_user_id"), raw.get("to_user_id")
            amt = integer_in_range(raw.get("amount"), 0, MAX_AMOUNT)
            note, vis = raw.get("note", ""), raw.get("visibility", "public")
            created_at = raw.get("created_at", reset_at)
            request_id = raw.get("request_id")
            if (not isinstance(pid, str) or not pid or len(pid) > 64 or
                pid in payment_ids or not isinstance(sender, str) or not isinstance(receiver, str) or
                sender not in temp["users"] or receiver not in temp["users"] or sender == receiver or
                amt is None or (amt == 0 and not isinstance(request_id, str)) or
                not isinstance(note, str) or len(note) > 200 or
                vis not in ("public", "private") or parse_timestamp(created_at) is None
                    or parse_timestamp(created_at) > reset_instant):
                fail(422, "validation_failed", "Invalid payment fixture")
            payment_ids.add(pid)
            item = {"id": pid, "from_user_id": sender, "to_user_id": receiver,
                "amount": amt, "note": note, "visibility": vis, "request_id": request_id,
                "settlement_id": raw.get("settlement_id"),
                "authorization_id": raw.get("authorization_id"),
                "refund_of": raw.get("refund_of"), "created_at": created_at}
            item["revisions"] = [self._initial_revision(amt, created_at)]
            temp["payments"].append(item)
        requests = fixture.get("requests", [])
        if not isinstance(requests, list):
            fail(422, "validation_failed", "requests must be an array")
        request_ids = set()
        for raw in requests:
            if not isinstance(raw, dict):
                fail(422, "validation_failed", "Invalid request fixture")
            rid, requester, payer = raw.get("id"), raw.get("requester_id"), raw.get("payer_id")
            amt = integer_in_range(raw.get("amount"), 0, MAX_AMOUNT)
            note, status = raw.get("note", ""), raw.get("status", "pending")
            created_at = raw.get("created_at", reset_at)
            if (not isinstance(rid, str) or not rid or len(rid) > 64 or
                rid in request_ids or not isinstance(requester, str) or not isinstance(payer, str) or
                requester not in temp["users"] or payer not in temp["users"] or requester == payer or
                amt is None or not isinstance(note, str) or len(note) > 200 or
                not isinstance(status, str) or status not in STATUSES or
                parse_timestamp(created_at) is None):
                fail(422, "validation_failed", "Invalid request fixture")
            request_ids.add(rid)
            temp["requests"].append({"id": rid, "requester_id": requester, "payer_id": payer,
                "amount": amt, "note": note, "status": status,
                "payment_id": raw.get("payment_id"), "created_at": created_at})
        authorizations = fixture.get("authorizations", [])
        if not isinstance(authorizations, list):
            fail(422, "validation_failed", "authorizations must be an array")
        authorization_ids = set()
        for raw in authorizations:
            if not isinstance(raw, dict):
                fail(422, "validation_failed", "Invalid authorization fixture")
            aid = raw.get("id")
            sender, receiver = raw.get("from_user_id"), raw.get("to_user_id")
            amount = integer_in_range(raw.get("amount"), 1, MAX_AMOUNT)
            note, vis, status = raw.get("note", ""), raw.get("visibility", "public"), raw.get("status")
            captured_default = amount if status == "captured" and amount is not None else 0
            captured = integer_in_range(raw.get("captured_amount", captured_default), 0, MAX_AMOUNT)
            expires_at = raw.get("expires_at")
            created_at = raw.get("created_at", reset_at)
            if (not isinstance(aid, str) or not aid or len(aid) > 64 or aid in authorization_ids
                    or not isinstance(sender, str) or sender not in temp["users"]
                    or not isinstance(receiver, str) or receiver not in temp["users"] or sender == receiver
                    or amount is None or captured is None or captured > amount
                    or not isinstance(note, str) or len(note) > 200
                    or not isinstance(vis, str) or vis not in ("public", "private")
                    or status not in ("open", "captured", "voided", "expired")
                    or parse_timestamp(expires_at) is None or parse_timestamp(created_at) is None):
                fail(422, "validation_failed", "Invalid authorization fixture")
            payment_ids = raw.get("payment_ids")
            if payment_ids is None:
                payment_ids = [raw["payment_id"]] if raw.get("payment_id") is not None else []
            payment_id = raw.get("payment_id", payment_ids[-1] if payment_ids else None)
            if (not isinstance(payment_ids, list)
                    or any(not isinstance(pid, str) or not pid for pid in payment_ids)
                    or len(set(payment_ids)) != len(payment_ids)
                    or (payment_id is not None and not isinstance(payment_id, str))
                    or (payment_ids and payment_id != payment_ids[-1])
                    or (not payment_ids and payment_id is not None)):
                fail(422, "validation_failed", "Invalid authorization payment links")
            if status == "open" and captured >= amount:
                fail(422, "validation_failed", "An open authorization must have a remainder")
            if status == "open" and parse_timestamp(expires_at) <= datetime.now(timezone.utc):
                status = "expired"
            authorization_ids.add(aid)
            closed_at = raw.get("closed_at")
            if closed_at is not None and parse_timestamp(closed_at) is None:
                fail(422, "validation_failed", "Invalid authorization fixture")
            if status == "open" and closed_at is not None:
                fail(422, "validation_failed", "An open authorization cannot have closed_at")
            if closed_at is None and status == "expired":
                closed_at = expires_at
            elif closed_at is None and status == "captured" and payment_ids:
                captured_payment = next(
                    (p for p in temp["payments"] if p["id"] == payment_ids[-1]), None)
                closed_at = captured_payment["created_at"] if captured_payment else created_at
            elif closed_at is None and status in ("captured", "voided"):
                closed_at = created_at
            item = {
                "id": aid, "from_user_id": sender, "to_user_id": receiver,
                "amount": amount, "captured_amount": captured, "note": note,
                "visibility": vis, "status": status, "expires_at": expires_at,
                "payment_id": payment_id, "payment_ids": list(payment_ids),
                "created_at": created_at, "closed_at": closed_at}
            item["capture_events"] = []
            for index, linked_id in enumerate(payment_ids):
                item["capture_events"].append({"payment_id": linked_id,
                    "final": status == "captured" and index == len(payment_ids) - 1})
            temp["authorizations"].append(item)
        auth_by_id = {a["id"]: a for a in temp["authorizations"]}
        payments_by_id = {p["id"]: p for p in temp["payments"]}
        held_by_user = {uid: 0 for uid in temp["users"]}
        for auth in temp["authorizations"]:
            linked = [p for p in temp["payments"] if p.get("authorization_id") == auth["id"]]
            if linked:
                if ([p["id"] for p in linked] != auth["payment_ids"]
                        or sum(p["amount"] for p in linked) != auth["captured_amount"]
                        or any(p["from_user_id"] != auth["from_user_id"]
                               or p["to_user_id"] != auth["to_user_id"] for p in linked)):
                    fail(422, "validation_failed", "Invalid authorization payment links")
            elif auth["payment_ids"]:
                fail(422, "validation_failed", "Invalid authorization payment links")
            if auth["status"] == "open":
                held_by_user[auth["from_user_id"]] += auth["amount"] - auth["captured_amount"]
        for payment in temp["payments"]:
            aid = payment.get("authorization_id")
            if aid is not None:
                if not isinstance(aid, str):
                    fail(422, "validation_failed", "Invalid authorization payment links")
                auth = auth_by_id.get(aid)
                if (auth is None or payment.get("request_id") is not None
                        or payment.get("settlement_id") is not None
                        or payment["id"] not in auth["payment_ids"]):
                    fail(422, "validation_failed", "Invalid authorization payment links")
        if any(held_by_user[uid] > temp["users"][uid]["balance"] for uid in held_by_user):
            fail(422, "validation_failed", "Seeded authorizations exceed available wallet funds")
        ops = fixture.get("settlement_operator_ids", [])
        if not isinstance(ops, list) or any(not isinstance(uid, str) or uid not in temp["users"] for uid in ops):
            fail(422, "validation_failed", "Invalid settlement operator list")
        temp["operators"] = list(dict.fromkeys(ops))
        try:
            validate_financial_links(temp["payments"], temp["requests"])
            validate_refund_links(temp["payments"])
        except ValueError:
            fail(422, "validation_failed", "Invalid financial links in fixture")
        net = {uid: 0 for uid in temp["users"]}
        for payment in temp["payments"]:
            net[payment["from_user_id"]] -= payment["amount"]
            net[payment["to_user_id"]] += payment["amount"]
        temp["opening_balances"] = {
            uid: user["balance"] - net[uid] for uid, user in temp["users"].items()}
        if any(balance < 0 for balance in temp["opening_balances"].values()):
            fail(422, "validation_failed", "Seeded payment history has a negative opening balance")
        for authorization in temp["authorizations"]:
            if authorization["status"] == "open" and parse_timestamp(authorization["expires_at"]) <= reset_instant:
                authorization["status"] = "expired"
                authorization["closed_at"] = authorization["expires_at"]
        prior_state = self.state
        self.state = temp
        try:
            if any(self._historical_total(uid, reset_instant) != user["balance"]
                   for uid, user in temp["users"].items()):
                fail(422, "validation_failed", "Seeded balances do not match payment history")
            if not self._record_historical_state_is_solvent():
                fail(422, "validation_failed", "Seeded history contains an overdraft")
        finally:
            self.state = prior_state
        return temp

    def _check_signup_available(self, email, handle):
        if email in self.state["emails"]:
            fail(409, "email_taken", "Email is already registered")
        if handle in self.state["handles"]:
            fail(409, "handle_taken", "Derived handle is already taken")

    def _signup(self, body, headers=None):
        email = required_string(body, "email")
        password = required_string(body, "password")
        display = required_string(body, "display_name")
        if not EMAIL_RE.fullmatch(email) or email.count("@") != 1:
            fail(422, "validation_failed", "Invalid email address")
        if len(password) < 8:
            fail(422, "validation_failed", "Password must contain at least 8 characters")
        remote = (headers or {}).get("_morrow_remote_address", "")
        stamp = time.time()
        if self.wallet_store.auth_attempts("signup-email", email.lower(), now=stamp,
                                           window_seconds=AUTH_RATE_WINDOW_SECONDS) >= AUTH_SIGNUP_MAX_ATTEMPTS:
            fail(429, "rate_limited", "Too many account creation attempts. Try again later.")
        if remote and self.wallet_store.auth_attempts("signup-ip", remote, now=stamp,
                                                       window_seconds=AUTH_RATE_WINDOW_SECONDS) >= AUTH_IP_SIGNUP_MAX_ATTEMPTS:
            fail(429, "rate_limited", "Too many account creation attempts. Try again later.")
        self.wallet_store.record_auth_attempt("signup-email", email.lower(), now=stamp,
                                              window_seconds=AUTH_RATE_WINDOW_SECONDS)
        if remote:
            self.wallet_store.record_auth_attempt("signup-ip", remote, now=stamp,
                                                  window_seconds=AUTH_RATE_WINDOW_SECONDS)
        local = email.split("@", 1)[0].lower()
        handle = re.sub(r"[^a-z0-9_]", "_", local)[:20]
        if not handle:
            handle = "_"
        with self.lock:
            self._check_signup_available(email, handle)
        with AUTH_HASH_SLOTS:
            hashed = password_hash(password)
        with self.lock:
            # Another signup may have taken the email or handle while this one hashed.
            self._check_signup_available(email, handle)
            uid, token = new_id("u"), secrets.token_urlsafe(32)
            registered_at = now()
            self.finance_store.ensure_financial_preferences(uid, registered_at)
            self.state["users"][uid] = {"id": uid, "email": email, "password_hash": hashed,
                                        "display_name": display, "handle": handle, "balance": 0}
            self.state["emails"][email] = uid
            self.state["handles"][handle] = uid
            self.state["opening_balances"][uid] = 0
            expires_at = (datetime.now(timezone.utc) + timedelta(seconds=AUTH_SESSION_TTL_SECONDS)).isoformat()
            return 201, {"user_id": uid, "display_name": display, "token": token,
                         "expires_at": expires_at}

    def _login(self, body, headers=None):
        email = required_string(body, "email")
        password = required_string(body, "password")
        normalized_email = email.strip().lower()
        remote = (headers or {}).get("_morrow_remote_address", "")
        stamp = time.time()
        if self.wallet_store.auth_attempts("login-email", normalized_email, now=stamp,
                                           window_seconds=AUTH_RATE_WINDOW_SECONDS) >= AUTH_LOGIN_MAX_ATTEMPTS:
            fail(429, "rate_limited", "Too many sign-in attempts. Try again later.")
        if remote and self.wallet_store.auth_attempts("login-ip", remote, now=stamp,
                                                       window_seconds=AUTH_RATE_WINDOW_SECONDS) >= AUTH_IP_LOGIN_MAX_ATTEMPTS:
            fail(429, "rate_limited", "Too many sign-in attempts. Try again later.")
        with self.lock:
            state = self.state
            uid = state["emails"].get(email)
            user = state["users"].get(uid) if uid else None
            encoded = user["password_hash"] if user else self.dummy_password_hash
        with AUTH_HASH_SLOTS:
            matches = password_matches(password, encoded)
        if user is None or not matches:
            self.wallet_store.record_auth_attempt("login-email", normalized_email, now=stamp,
                                                  window_seconds=AUTH_RATE_WINDOW_SECONDS)
            if remote:
                self.wallet_store.record_auth_attempt("login-ip", remote, now=stamp,
                                                      window_seconds=AUTH_RATE_WINDOW_SECONDS)
            fail(401, "unauthenticated", "Email or password is incorrect")
        self.wallet_store.clear_auth_attempts("login-email", normalized_email)
        with self.lock:
            # A reset or import may have replaced this user while the password was checked.
            if self.state is not state or state["users"].get(uid) is not user:
                fail(401, "unauthenticated", "Email or password is incorrect")
            token = secrets.token_urlsafe(32)
            expires_at = (datetime.now(timezone.utc) + timedelta(seconds=AUTH_SESSION_TTL_SECONDS)).isoformat()
            return 200, {"user_id": uid, "display_name": user["display_name"],
                         "token": token, "expires_at": expires_at}

    def dispatch(self, method, target, headers, raw_body):
        path = urlsplit(target).path
        if method == "POST" and path in ("/auth/login", "/auth/signup"):
            return self._dispatch_auth(path, headers, raw_body)
        if path == "/ai/companion":
            latest = self.wallet_store.load()
            if latest is not None:
                with self.lock:
                    self.state = latest
            return self._dispatch_inner(method, target, headers, raw_body)

        api_error = None
        result = None
        with self.wallet_store.transaction() as persisted:
            original = copy.deepcopy(persisted)
            with self.lock:
                self.state = persisted
            try:
                result = self._dispatch_inner(method, target, headers, raw_body)
            except ApiError as exc:
                with self.lock:
                    self.state = original
                if path in ("/auth/login", "/auth/signup"):
                    # Failed credential attempts are data too; commit their throttle
                    # counters while rolling back any partial application state.
                    api_error = exc
                else:
                    raise
            except Exception:
                with self.lock:
                    self.state = original
                raise
            else:
                if self.state != original:
                    self._reconcile_wallet_state()
                    self.wallet_store.save(self.state)
        if api_error is not None:
            with self.lock:
                self.state = persisted
        if api_error is not None:
            raise api_error
        return result

    def _dispatch_auth(self, path, headers, raw_body):
        try:
            body = json.loads(raw_body.decode("utf-8"), parse_int=parse_json_integer,
                              parse_float=parse_json_decimal,
                              parse_constant=lambda _: (_ for _ in ()).throw(ValueError())) if raw_body else {}
        except (UnicodeDecodeError, json.JSONDecodeError, InvalidOperation, ValueError, RecursionError):
            fail(400, "malformed_request", "Request body is not valid JSON")
        if not isinstance(body, dict) or json_too_deep(body):
            fail(400, "malformed_request", "Request body must be a JSON object")
        if path == "/auth/login":
            return self._durable_login(body, headers)
        return self._durable_signup(body, headers)

    def _auth_limit(self, kind, identifier, limit, stamp):
        if self.wallet_store.auth_attempts(kind, identifier, now=stamp,
                                           window_seconds=AUTH_RATE_WINDOW_SECONDS) >= limit:
            fail(429, "rate_limited", "Too many authentication attempts. Try again later.")

    def _durable_login(self, body, headers):
        email = required_string(body, "email")
        password = required_string(body, "password")
        normalized = email.strip().lower()
        remote = headers.get("_morrow_remote_address", "")
        stamp = time.time()
        with self.wallet_store.transaction() as persisted:
            with self.lock:
                self.state = persisted
                self._auth_limit("login-email", normalized, AUTH_LOGIN_MAX_ATTEMPTS, stamp)
                if remote:
                    self._auth_limit("login-ip", remote, AUTH_IP_LOGIN_MAX_ATTEMPTS, stamp)
                uid = self.state["emails"].get(email)
                user = self.state["users"].get(uid) if uid else None
                encoded = user["password_hash"] if user else self.dummy_password_hash

        with AUTH_HASH_SLOTS:
            matches = password_matches(password, encoded)
        if user is None or not matches:
            with self.wallet_store.transaction():
                self.wallet_store.record_auth_attempt("login-email", normalized, now=stamp,
                                                      window_seconds=AUTH_RATE_WINDOW_SECONDS)
                if remote:
                    self.wallet_store.record_auth_attempt("login-ip", remote, now=stamp,
                                                          window_seconds=AUTH_RATE_WINDOW_SECONDS)
            fail(401, "unauthenticated", "Email or password is incorrect")

        with self.wallet_store.transaction() as persisted:
            with self.lock:
                self.state = persisted
                current = self.state["users"].get(uid)
                if current is None or current["password_hash"] != encoded:
                    fail(401, "unauthenticated", "Email or password is incorrect")
                self.wallet_store.clear_auth_attempts("login-email", normalized)
                token = secrets.token_urlsafe(32)
                expires_at = (datetime.now(timezone.utc) + timedelta(seconds=AUTH_SESSION_TTL_SECONDS)).isoformat()
                self.wallet_store.create_session(token, uid, expires_at=expires_at)
                self._reconcile_wallet_state()
                self.wallet_store.save(self.state)
                return 200, {"user_id": uid, "display_name": current["display_name"],
                             "token": token, "expires_at": expires_at}

    def _durable_signup(self, body, headers):
        email = required_string(body, "email")
        password = required_string(body, "password")
        display = required_string(body, "display_name")
        requested_role = body.get("role", "CUSTOMER")
        
        # Validate role (cannot self-assign PLATFORM_ADMIN)
        if requested_role not in VALID_SIGNUP_ROLES:
            fail(422, "validation_failed", f"Invalid role. Allowed: {', '.join(VALID_SIGNUP_ROLES)}")
        
        if not EMAIL_RE.fullmatch(email) or email.count("@") != 1:
            fail(422, "validation_failed", "Invalid email address")
        if len(password) < 8 or len(password) > 1024:
            fail(422, "validation_failed", "Password must contain 8 to 1,024 characters")
        local = email.split("@", 1)[0].lower()
        handle = re.sub(r"[^a-z0-9_]", "_", local)[:20] or "_"
        remote = headers.get("_morrow_remote_address", "")
        stamp = time.time()
        with self.wallet_store.transaction() as persisted:
            with self.lock:
                self.state = persisted
                # Preserve the established duplicate-account response under races;
                # rate limits still apply before recording attempts for new accounts.
                self._check_signup_available(email, handle)
                self._auth_limit("signup-email", email.lower(), AUTH_SIGNUP_MAX_ATTEMPTS, stamp)
                if remote:
                    self._auth_limit("signup-ip", remote, AUTH_IP_SIGNUP_MAX_ATTEMPTS, stamp)
                self.wallet_store.record_auth_attempt("signup-email", email.lower(), now=stamp,
                                                      window_seconds=AUTH_RATE_WINDOW_SECONDS)
                if remote:
                    self.wallet_store.record_auth_attempt("signup-ip", remote, now=stamp,
                                                          window_seconds=AUTH_RATE_WINDOW_SECONDS)

        with AUTH_HASH_SLOTS:
            hashed = password_hash(password)

        with self.wallet_store.transaction() as persisted:
            with self.lock:
                self.state = persisted
                self._check_signup_available(email, handle)
                uid, token = new_id("u"), secrets.token_urlsafe(32)
                registered_at = now()
                self.finance_store.ensure_financial_preferences(uid, registered_at)
                
                # Create user with requested role
                user_obj = {
                    "id": uid,
                    "email": email,
                    "password_hash": hashed,
                    "display_name": display,
                    "handle": handle,
                    "balance": 0,
                    "roles": [requested_role]
                }
                
                # If requesting PAYMENT_ANALYST role, set status to pending
                if requested_role == "PAYMENT_ANALYST":
                    user_obj["analyst_status"] = "pending"
                    user_obj["analyst_requested_at"] = registered_at
                
                self.state["users"][uid] = user_obj
                self.state["emails"][email] = uid
                self.state["handles"][handle] = uid
                self.state["opening_balances"][uid] = 0
                expires_at = (datetime.now(timezone.utc) + timedelta(seconds=AUTH_SESSION_TTL_SECONDS)).isoformat()
                self.wallet_store.create_session(token, uid, expires_at=expires_at)
                self._reconcile_wallet_state()
                self.wallet_store.save(self.state)
                
                response = {
                    "user_id": uid,
                    "display_name": display,
                    "token": token,
                    "expires_at": expires_at,
                    "roles": [requested_role]
                }
                
                if requested_role == "PAYMENT_ANALYST":
                    response["analyst_status"] = "pending"
                
                return 201, response

    # Analyst API methods
    def _analyst_overview(self, uid):
        """Get aggregated payment analytics (approved analysts only)."""
        user = self.state["users"].get(uid)
        has_access, error = require_analyst(user)
        if not has_access:
            fail(403, "forbidden", error)
        
        with closing(self.paypal_store._connect()) as db:
            # Get all checkouts (not user-scoped for analysts)
            rows = [dict(row) for row in db.execute(
                "SELECT * FROM paypal_checkouts ORDER BY created_at DESC"
            ).fetchall()]
        
        # Calculate metrics by currency
        captured_revenue = {}
        pending_count = 0
        failed_count = 0
        captured_count = 0
        
        for row in rows:
            currency = row["currency"]
            status = row["status"]
            amount = row["amount_minor"]
            
            if status == "COMPLETED":
                captured_revenue[currency] = captured_revenue.get(currency, 0) + amount
                captured_count += 1
            elif status in ("CREATED", "APPROVED", "CAPTURE_IN_PROGRESS", "CAPTURE_PENDING"):
                pending_count += 1
            elif status in ("CAPTURE_FAILED", "FAILED"):
                failed_count += 1
        
        # Calculate success rate (null if no completed attempts)
        total_completed = captured_count + failed_count
        success_rate = None
        if total_completed > 0:
            success_rate = round((captured_count * 100.0) / total_completed, 2)
        
        # Average transaction value by currency
        avg_value = {}
        for currency in captured_revenue:
            captured_in_currency = [r for r in rows if r["status"] == "COMPLETED" and r["currency"] == currency]
            if captured_in_currency:
                avg_value[currency] = captured_revenue[currency] // len(captured_in_currency)
        
        return 200, {
            "captured_revenue": captured_revenue,
            "pending_transactions": pending_count,
            "failed_attempts": failed_count,
            "success_rate": success_rate,
            "total_transactions": len(rows),
            "average_transaction_value": avg_value
        }
    
    def _analyst_transactions(self, uid, query):
        """List transactions with pagination and filters (approved analysts only)."""
        user = self.state["users"].get(uid)
        has_access, error = require_analyst(user)
        if not has_access:
            fail(403, "forbidden", error)
        
        # Validate pagination
        try:
            limit = int(query.get("limit", ["50"])[0])
            if not (1 <= limit <= 100):
                fail(400, "validation_failed", "limit must be between 1 and 100")
        except ValueError:
            fail(400, "validation_failed", "limit must be an integer")
        
        try:
            offset = int(query.get("offset", ["0"])[0])
            if not (0 <= offset <= 10000):
                fail(400, "validation_failed", "offset must be between 0 and 10000")
        except ValueError:
            fail(400, "validation_failed", "offset must be an integer")
        
        # Validate filters
        status_filter = query.get("status", [None])[0]
        valid_statuses = {"CREATING", "CREATED", "APPROVED", "COMPLETED", "CAPTURE_FAILED", "FAILED", "VOIDED"}
        if status_filter and status_filter not in valid_statuses:
            fail(400, "validation_failed", f"Invalid status. Allowed: {', '.join(valid_statuses)}")
        
        currency_filter = query.get("currency", [None])[0]
        if currency_filter and currency_filter not in ("EUR", "USD", "GBP", "JPY"):
            fail(400, "validation_failed", "Invalid currency. Allowed: EUR, USD, GBP, JPY")
        
        start_date = query.get("start_date", [None])[0]
        end_date = query.get("end_date", [None])[0]
        
        if start_date and end_date:
            try:
                start_dt = datetime.fromisoformat(start_date.replace('Z', '+00:00'))
                end_dt = datetime.fromisoformat(end_date.replace('Z', '+00:00'))
                if start_dt > end_dt:
                    fail(400, "validation_failed", "start_date must be before or equal to end_date")
                if (end_dt - start_dt).days > 365:
                    fail(400, "validation_failed", "Date range cannot exceed 365 days")
            except ValueError:
                fail(400, "validation_failed", "Invalid date format. Use ISO 8601 (YYYY-MM-DD)")
        
        # Fetch all checkouts
        with closing(self.paypal_store._connect()) as db:
            rows = [dict(row) for row in db.execute(
                "SELECT * FROM paypal_checkouts ORDER BY created_at DESC"
            ).fetchall()]
        
        # Apply filters
        if status_filter:
            rows = [r for r in rows if r["status"] == status_filter]
        if currency_filter:
            rows = [r for r in rows if r["currency"] == currency_filter]
        if start_date:
            rows = [r for r in rows if r["created_at"] >= start_date]
        if end_date:
            rows = [r for r in rows if r["created_at"] <= end_date]
        
        total_count = len(rows)
        rows = rows[offset:offset + limit]
        
        # Build response (privacy: only buyer_id exposed, not email/display_name)
        transactions = []
        for r in rows:
            transactions.append({
                "checkout_id": r["checkout_id"],
                "buyer_id": r["user_id"],
                "amount_minor": r["amount_minor"],
                "currency": r["currency"],
                "status": r["status"],
                "note": r["note"],
                "created_at": r["created_at"],
                "updated_at": r["updated_at"]
            })
        
        return 200, {
            "transactions": transactions,
            "total_count": total_count,
            "limit": limit,
            "offset": offset,
            "has_more": offset + limit < total_count
        }
    
    def _analyst_analytics(self, uid, query):
        """Get daily revenue trends (approved analysts only)."""
        user = self.state["users"].get(uid)
        has_access, error = require_analyst(user)
        if not has_access:
            fail(403, "forbidden", error)
        
        # Validate days parameter
        try:
            days = int(query.get("days", ["30"])[0])
            if not (1 <= days <= 90):
                fail(400, "validation_failed", "days must be between 1 and 90")
        except ValueError:
            fail(400, "validation_failed", "days must be an integer")
        
        # Fetch completed checkouts
        with closing(self.paypal_store._connect()) as db:
            rows = [dict(row) for row in db.execute(
                "SELECT * FROM paypal_checkouts WHERE status='COMPLETED' ORDER BY created_at DESC"
            ).fetchall()]
        
        # Group by date and currency
        daily_data = {}
        for row in rows:
            date_key = row["created_at"][:10]  # Extract YYYY-MM-DD
            currency = row["currency"]
            key = (date_key, currency)
            
            if key not in daily_data:
                daily_data[key] = {"revenue_minor": 0, "transaction_count": 0}
            
            daily_data[key]["revenue_minor"] += row["amount_minor"]
            daily_data[key]["transaction_count"] += 1
        
        # Build response
        daily_revenue = [
            {
                "date": date_key,
                "currency": currency,
                "revenue_minor": data["revenue_minor"],
                "transaction_count": data["transaction_count"]
            }
            for (date_key, currency), data in sorted(daily_data.items(), reverse=True)[:days * 4]  # Allow multiple currencies per day
        ]
        
        return 200, {
            "daily_revenue": daily_revenue,
            "days_requested": days
        }
    
    # Admin API methods
    def _admin_list_analysts(self, uid, query):
        """List all analyst accounts (admin only)."""
        user = self.state["users"].get(uid)
        has_access, error = require_admin(user)
        if not has_access:
            fail(403, "forbidden", error)
        
        # Validate pagination
        try:
            limit = int(query.get("limit", ["50"])[0])
            if not (1 <= limit <= 100):
                fail(400, "validation_failed", "limit must be between 1 and 100")
        except ValueError:
            fail(400, "validation_failed", "limit must be an integer")
        
        try:
            offset = int(query.get("offset", ["0"])[0])
            if not (0 <= offset <= 10000):
                fail(400, "validation_failed", "offset must be between 0 and 10000")
        except ValueError:
            fail(400, "validation_failed", "offset must be an integer")
        
        # Validate status filter
        status_filter = query.get("status", [None])[0]
        if status_filter and status_filter not in ("pending", "approved", "suspended", "rejected"):
            fail(400, "validation_failed", "Invalid status. Allowed: pending, approved, suspended, rejected")
        
        # Find all analysts
        analysts = []
        for user_obj in self.state["users"].values():
            if "PAYMENT_ANALYST" in user_obj.get("roles", []):
                analyst_status = user_obj.get("analyst_status", "pending")
                
                if status_filter and analyst_status != status_filter:
                    continue
                
                analysts.append({
                    "user_id": user_obj["id"],
                    "email": user_obj["email"],  # Admin can see emails
                    "display_name": user_obj["display_name"],
                    "handle": user_obj["handle"],
                    "analyst_status": analyst_status,
                    "analyst_requested_at": user_obj.get("analyst_requested_at"),
                    "analyst_approved_by": user_obj.get("analyst_approved_by"),
                    "analyst_approved_at": user_obj.get("analyst_approved_at"),
                    "analyst_suspended_by": user_obj.get("analyst_suspended_by"),
                    "analyst_suspended_at": user_obj.get("analyst_suspended_at"),
                    "analyst_suspension_reason": user_obj.get("analyst_suspension_reason")
                })
        
        # Sort by requested date (newest first)
        analysts.sort(key=lambda a: a.get("analyst_requested_at") or "", reverse=True)
        
        total_count = len(analysts)
        analysts = analysts[offset:offset + limit]
        
        return 200, {
            "analysts": analysts,
            "total_count": total_count,
            "limit": limit,
            "offset": offset,
            "has_more": offset + limit < total_count
        }
    
    def _admin_approve_analyst(self, admin_uid, target_user_id):
        """Approve pending analyst (admin only)."""
        user = self.state["users"].get(admin_uid)
        has_access, error = require_admin(user)
        if not has_access:
            fail(403, "forbidden", error)
        
        target_user = self.state["users"].get(target_user_id)
        if not target_user:
            fail(404, "not_found", "User not found")
        
        if "PAYMENT_ANALYST" not in target_user.get("roles", []):
            fail(422, "validation_failed", "User does not have PAYMENT_ANALYST role")
        
        previous_status = target_user.get("analyst_status", "pending")
        if previous_status == "approved":
            fail(422, "validation_failed", "Analyst already approved")
        
        # Update user status
        target_user["analyst_status"] = "approved"
        target_user["analyst_approved_by"] = admin_uid
        target_user["analyst_approved_at"] = now()
        
        # Log admin action
        action_id = f"act_{secrets.token_urlsafe(16)}"
        self.wallet_store.log_admin_action(
            action_id=action_id,
            action_type="approve_analyst",
            actor_user_id=admin_uid,
            target_user_id=target_user_id,
            reason=None,
            metadata={"previous_status": previous_status}
        )
        
        return 200, {
            "success": True,
            "analyst": {
                "user_id": target_user_id,
                "analyst_status": "approved",
                "approved_by": admin_uid,
                "approved_at": target_user["analyst_approved_at"]
            }
        }
    
    def _admin_suspend_analyst(self, admin_uid, target_user_id, body):
        """Suspend approved analyst (admin only)."""
        user = self.state["users"].get(admin_uid)
        has_access, error = require_admin(user)
        if not has_access:
            fail(403, "forbidden", error)
        
        reason = body.get("reason", "").strip()
        if not reason:
            fail(422, "validation_failed", "Suspension reason required")
        
        target_user = self.state["users"].get(target_user_id)
        if not target_user:
            fail(404, "not_found", "User not found")
        
        if "PAYMENT_ANALYST" not in target_user.get("roles", []):
            fail(422, "validation_failed", "User does not have PAYMENT_ANALYST role")
        
        previous_status = target_user.get("analyst_status", "pending")
        if previous_status == "suspended":
            fail(422, "validation_failed", "Analyst already suspended")
        
        # Update user status
        target_user["analyst_status"] = "suspended"
        target_user["analyst_suspended_by"] = admin_uid
        target_user["analyst_suspended_at"] = now()
        target_user["analyst_suspension_reason"] = reason
        
        # Log admin action
        action_id = f"act_{secrets.token_urlsafe(16)}"
        self.wallet_store.log_admin_action(
            action_id=action_id,
            action_type="suspend_analyst",
            actor_user_id=admin_uid,
            target_user_id=target_user_id,
            reason=reason,
            metadata={"previous_status": previous_status}
        )
        
        return 200, {
            "success": True,
            "analyst": {
                "user_id": target_user_id,
                "analyst_status": "suspended",
                "suspended_by": admin_uid,
                "suspended_at": target_user["analyst_suspended_at"],
                "suspension_reason": reason
            }
        }
    
    def _admin_reinstate_analyst(self, admin_uid, target_user_id):
        """Reinstate suspended analyst (admin only)."""
        user = self.state["users"].get(admin_uid)
        has_access, error = require_admin(user)
        if not has_access:
            fail(403, "forbidden", error)
        
        target_user = self.state["users"].get(target_user_id)
        if not target_user:
            fail(404, "not_found", "User not found")
        
        if "PAYMENT_ANALYST" not in target_user.get("roles", []):
            fail(422, "validation_failed", "User does not have PAYMENT_ANALYST role")
        
        previous_status = target_user.get("analyst_status", "pending")
        if previous_status != "suspended":
            fail(422, "validation_failed", "Analyst is not suspended")
        
        # Update user status
        target_user["analyst_status"] = "approved"
        target_user["analyst_approved_by"] = admin_uid
        target_user["analyst_approved_at"] = now()
        
        # Log admin action
        action_id = f"act_{secrets.token_urlsafe(16)}"
        self.wallet_store.log_admin_action(
            action_id=action_id,
            action_type="reinstate_analyst",
            actor_user_id=admin_uid,
            target_user_id=target_user_id,
            reason=None,
            metadata={"previous_status": previous_status}
        )
        
        return 200, {
            "success": True,
            "analyst": {
                "user_id": target_user_id,
                "analyst_status": "approved",
                "approved_by": admin_uid,
                "approved_at": target_user["analyst_approved_at"]
            }
        }

    def _reconcile_wallet_state(self):
        document = {"track": "pocketful", "format_version": 1,
                    "state": self._serialize_state()}
        validated = self._validate_import(document)
        if validated != self.state:
            raise RuntimeError("Wallet persistence reconciliation changed canonical state")

    def _dispatch_inner(self, method, target, headers, raw_body):
        read_at = datetime.now(timezone.utc)
        parsed = urlsplit(target)
        path = parsed.path
        try:
            body = json.loads(raw_body.decode("utf-8"), parse_int=parse_json_integer,
                              parse_float=parse_json_decimal,
                              parse_constant=lambda _: (_ for _ in ()).throw(ValueError())) if raw_body else {}
        except (UnicodeDecodeError, json.JSONDecodeError, InvalidOperation, ValueError, RecursionError):
            fail(400, "malformed_request", "Request body is not valid JSON")
        if json_too_deep(body):
            fail(400, "malformed_request", "Request body is nested too deeply")
        if not isinstance(body, dict):
            fail(400, "malformed_request", "Request body must be a JSON object")
        query = parse_qs(parsed.query, keep_blank_values=True)
        if method == "POST" and path in ("/auth/signup", "/auth/login"):
            # scrypt is deliberately slow, so it runs outside the service lock; the lock
            # only guards the lookups and the final write.
            return self._signup(body, headers) if path.endswith("signup") else self._login(body, headers)
        with self.lock:
            if path.startswith("/_test/") and os.environ.get("POCKETFUL_ENABLE_TEST_ROUTES") != "1":
                fail(404, "not_found", "Endpoint not found")
            if method == "GET" and path == "/health":
                health = self.wallet_store.health()
                return (200 if health["status"] == "ok" else 503,
                        {"status": health["status"], "checks": {"wallet_store": health}})
            if method == "POST" and path == "/_test/reset":
                replacement = self._reset(body)
                coverage = body.get("forecast_coverage_started_at", {})
                if (not isinstance(coverage, dict)
                        or any(uid not in replacement["users"] or parse_timestamp(value) is None
                               or parse_timestamp(value) > read_at
                               for uid, value in coverage.items())):
                    fail(422, "validation_failed", "Invalid forecast coverage fixture")
                self.state = replacement
                self.statement_snapshots.clear()
                self.ai_proposals.clear()
                self.companion_requests.clear()
                self.finance_store.reset_for_tests()
                self.wallet_store.reset_auth_attempts_for_tests()
                reset_coverage_at = now()
                for uid in replacement["users"]:
                    self.finance_store.ensure_financial_preferences(
                        uid, coverage.get(uid, reset_coverage_at))
                return 204, None
            if method == "GET" and path == "/_test/export":
                self._expire_authorizations()
                return 200, {"track": "pocketful", "format_version": 1,
                             "state": self._serialize_state(),
                             "statement_snapshots": copy.deepcopy(self.statement_snapshots)}
            if method == "POST" and path == "/_test/import":
                replacement = self._validate_import(body)
                snapshots = self._validate_snapshots(body, replacement)
                expires_at = (datetime.now(timezone.utc)
                              + timedelta(seconds=AUTH_SESSION_TTL_SECONDS)).isoformat()
                for token, uid in replacement.get("tokens", {}).items():
                    self.wallet_store.create_session(token, uid, expires_at=expires_at)
                replacement["tokens"] = {}
                self.state = replacement
                self.statement_snapshots = snapshots
                return 204, None
            if method == "POST" and path == "/webhooks/paypal":
                return self._paypal_webhook(headers, body, raw_body)
            token_header = headers.get("Authorization", "")
            if not token_header.startswith("Bearer ") or not token_header[7:]:
                fail(401, "unauthenticated", "A bearer token is required")
            presented_token = token_header[7:]
            session = self.wallet_store.session(presented_token, now=read_at.isoformat())
            uid = session["user_id"] if session is not None else None
            if uid not in self.state["users"]:
                uid = None
            if uid is None:
                fail(401, "unauthenticated", "Session is invalid or expired")
            self._expire_authorizations()

            if method == "POST" and path == "/auth/logout":
                self.wallet_store.revoke_session(presented_token, now=read_at.isoformat())
                return 200, {"logged_out": True}

            if method == "GET" and path == "/me":
                return 200, self._me_at(uid, query, read_at)
            
            # Analyst endpoints
            if method == "GET" and path == "/api/analyst/overview":
                return self._analyst_overview(uid)
            if method == "GET" and path == "/api/analyst/transactions":
                return self._analyst_transactions(uid, query)
            if method == "GET" and path == "/api/analyst/analytics":
                return self._analyst_analytics(uid, query)
            
            # Admin endpoints
            if method == "GET" and path == "/api/admin/analysts":
                return self._admin_list_analysts(uid, query)
            
            analyst_match = re.fullmatch(r"/api/admin/analysts/([^/]+)/(approve|suspend|reinstate)", path)
            if method == "POST" and analyst_match:
                target_user_id, action = analyst_match.groups()
                if action == "approve":
                    return self._admin_approve_analyst(uid, target_user_id)
                elif action == "suspend":
                    return self._admin_suspend_analyst(uid, target_user_id, body)
                elif action == "reinstate":
                    return self._admin_reinstate_analyst(uid, target_user_id)
            
            if method == "GET" and path == "/forecast/settings":
                row = self.finance_store.ensure_financial_preferences(uid)
                return 200, self._financial_preferences_api(
                    row, self.state["currency"], self.state["minor_units"])
            if method == "PATCH" and path == "/forecast/settings":
                return 200, self._update_financial_preferences(uid, body, headers)
            if method == "GET" and path in ("/forecast", "/safe-to-spend"):
                return 200, self._calculate_financial_outlook(uid, read_at)
            if path == "/commitments" and method == "GET":
                status_filter = query.get("status", [None])[0]
                if status_filter not in (None, "active", "paused", "cancelled"):
                    fail(422, "validation_failed", "Invalid commitment status")
                limit = page_value(query, "limit", 50, 1, 200)
                offset = page_value(query, "offset", 0, 0)
                result = self.finance_store.list_commitments(uid, status_filter, limit, offset)
                return 200, {"commitments": [self._commitment_api(item) for item in result["commitments"]],
                             "has_more": result["has_more"]}
            if path == "/commitments" and method == "POST":
                return self._create_commitment(uid, body, headers)
            commitment_match = re.fullmatch(r"/commitments/([^/]+)(?:/(occurrences(?:/([^/]+)/pay)?|pause|resume|cancel))?", path)
            if commitment_match:
                commitment_id, action, occurrence_id = commitment_match.groups()
                if method == "GET" and action is None:
                    row = self._finance_call(lambda: self.finance_store.get_commitment(uid, commitment_id))
                    return 200, self._commitment_api(row)
                if method == "PATCH" and action is None:
                    return self._update_commitment(uid, commitment_id, body, headers)
                if method == "GET" and action == "occurrences":
                    status_filter = query.get("status", [None])[0]
                    if status_filter not in (None, "unpaid", "paid", "paused", "cancelled"):
                        fail(422, "validation_failed", "Invalid occurrence status")
                    limit = page_value(query, "limit", 50, 1, 200)
                    offset = page_value(query, "offset", 0, 0)
                    result = self._finance_call(lambda: self.finance_store.list_occurrences(
                        uid, commitment_id, status_filter, limit, offset))
                    return 200, result
                if method == "POST" and action in ("pause", "resume", "cancel"):
                    return self._commitment_status(uid, commitment_id, action, body, headers)
                if method == "POST" and occurrence_id and action == f"occurrences/{occurrence_id}/pay":
                    return self._pay_commitment_occurrence(uid, commitment_id, occurrence_id, body, headers)

            if path == "/shared-expenses" and method == "GET":
                status_filter = query.get("status", [None])[0]
                if status_filter not in (None, "open", "settled"):
                    fail(422, "validation_failed", "Invalid shared expense status")
                limit = page_value(query, "limit", 50, 1, 200)
                offset = page_value(query, "offset", 0, 0)
                result = self.finance_store.list_shared_expenses(uid, status_filter, limit, offset)
                return 200, {"expenses": [self._shared_expense_api(item) for item in result["expenses"]],
                             "has_more": result["has_more"]}
            if path == "/shared-expenses" and method == "POST":
                return self._create_shared_expense(uid, body, headers)
            expense_match = re.fullmatch(r"/shared-expenses/([^/]+)(?:/(settlements))?", path)
            if expense_match:
                expense_id, action = expense_match.groups()
                if method == "GET" and action is None:
                    row = self._finance_call(lambda: self.finance_store.get_shared_expense(uid, expense_id))
                    return 200, self._shared_expense_api(row)
                if method == "GET" and action == "settlements":
                    result = self._finance_call(lambda: self.finance_store.list_settlements(uid, expense_id))
                    expense = self._finance_call(lambda: self.finance_store.get_shared_expense(uid, expense_id))
                    handles = {item["participant_user_id"]: item["participant_handle"]
                               for item in expense["participants"]}
                    settlements = [{"settlement_id": row["settlement_id"],
                                    "participant_handle": handles[row["participant_user_id"]],
                                    "amount_minor": row["amount_minor"], "source": row["source"],
                                    "payment_reference": row["payment_reference"],
                                    "created_at": row["created_at"]}
                                   for row in result["settlements"]]
                    return 200, {"settlements": settlements}
                if method == "POST" and action == "settlements":
                    return self._settle_shared_expense(uid, expense_id, body, headers)
            if method == "POST" and path == "/paypal/checkouts":
                return self._start_paypal_checkout(uid, body, headers)
            if method == "GET" and path == "/paypal/checkouts":
                return 200, {"checkouts": [self._checkout_obj(row) for row in
                                             self.paypal_store.list_for_user(uid)]}
            checkout_match = re.fullmatch(r"/paypal/checkouts/([^/]+)(?:/(capture|reconcile))?", path)
            if checkout_match:
                checkout_id, action = checkout_match.groups()
                if method == "GET" and action is None:
                    row = self.paypal_store.get(checkout_id)
                    if row is None or row["user_id"] != uid:
                        fail(404, "not_found", "Checkout not found")
                    return 200, self._checkout_obj(row)
                if method == "POST" and action == "capture":
                    return self._capture_paypal_checkout(uid, checkout_id, headers)
                if method == "POST" and action == "reconcile":
                    return self._reconcile_paypal_checkout(uid, checkout_id)
            if method == "POST" and path == "/ai/proposals":
                return self._assistant_proposal(uid, body)
            if method == "POST" and path == "/ai/companion":
                return self._financial_companion(uid, body, read_at)
            confirmation_match = re.fullmatch(r"/ai/proposals/([^/]+)/confirm", path)
            if method == "POST" and confirmation_match:
                return self._confirm_assistant_proposal(uid, confirmation_match.group(1),
                                                        body, headers)
            if method == "POST":
                correction_match = re.fullmatch(r"/payments/([^/]+)/corrections", path)
                if correction_match:
                    return self._correct_payment(uid, correction_match.group(1), body, headers)
                refund_match = re.fullmatch(r"/payments/([^/]+)/refunds", path)
                if refund_match or path == "/correction-batches":
                    status, replay, signature = self._key_and_replay(headers, uid, method, path, body)
                    if status is not None:
                        return status, replay
                    if refund_match:
                        return self._refund_payment(uid, refund_match.group(1), body, signature)
                    return self._correct_batch(uid, body, signature)
            if method == "GET":
                revisions_match = re.fullmatch(r"/payments/([^/]+)/revisions", path)
                if revisions_match:
                    payment = next((p for p in self.state["payments"]
                                    if p["id"] == revisions_match.group(1)), None)
                    if payment is None or uid not in (
                            payment["from_user_id"], payment["to_user_id"]):
                        fail(404, "not_found", "Payment not found")
                    revisions = copy.deepcopy(self._payment_revisions(payment))
                    for revision in revisions:
                        revision.setdefault("correction_batch_id", None)
                    return 200, {"revisions": revisions}
            if method == "POST" and path in ("/payments", "/requests", "/splits", "/settlements",
                                               "/authorizations"):
                status, replay, signature = self._key_and_replay(headers, uid, method, path, body)
                if status is not None:
                    return status, replay
                if path == "/payments":
                    return self._create_payment(uid, body, signature)
                if path == "/requests":
                    return self._create_request(uid, body, signature)
                if path == "/splits":
                    return self._create_split(uid, body, signature)
                if path == "/authorizations":
                    return self._create_authorization(uid, body, signature)
                return self._settle(uid, body, signature)

            m = re.fullmatch(r"/requests/([^/]+)/(pay|decline|cancel)", path)
            if method == "POST" and m:
                rid, action = m.group(1), m.group(2)
                if action == "pay":
                    status, replay, signature = self._key_and_replay(headers, uid, method, path, body)
                    if status is not None:
                        return status, replay
                    return self._pay_request(uid, rid, body, signature)
                item = next((r for r in self.state["requests"] if r["id"] == rid), None)
                if item is None:
                    fail(404, "not_found", "Request not found")
                owner = item["payer_id"] if action == "decline" else item["requester_id"]
                if owner != uid:
                    fail(403, "forbidden", "You may not change this request")
                desired = "declined" if action == "decline" else "cancelled"
                if item["status"] == desired:
                    return 200, self._request_obj(item)
                if item["status"] != "pending":
                    fail(409, "request_not_pending", "Request is not pending")
                item["status"] = desired
                return 200, self._request_obj(item)

            authorization_match = re.fullmatch(r"/authorizations/([^/]+)/(capture|void)", path)
            if method == "POST" and authorization_match:
                authorization_id, action = authorization_match.groups()
                if action == "capture":
                    status, replay, signature = self._key_and_replay(headers, uid, method, path, body)
                    if status is not None:
                        return status, replay
                    return self._capture_authorization(uid, authorization_id, body, signature)
                return self._void_authorization(uid, authorization_id)

            if method == "GET" and path == "/requests":
                direction = query.get("direction", [None])[0]
                status_filter = query.get("status", [None])[0]
                if direction not in (None, "incoming", "outgoing"):
                    fail(422, "validation_failed", "Invalid direction")
                if status_filter not in (None, *STATUSES):
                    fail(422, "validation_failed", "Invalid status")
                limit = page_value(query, "limit", 50, 1, 200)
                offset = page_value(query, "offset", 0, 0)
                rows = [r for r in self.state["requests"] if uid in (r["requester_id"], r["payer_id"])]
                if direction == "incoming":
                    rows = [r for r in rows if r["payer_id"] == uid]
                elif direction == "outgoing":
                    rows = [r for r in rows if r["requester_id"] == uid]
                if status_filter is not None:
                    rows = [r for r in rows if r["status"] == status_filter]
                rows.sort(key=lambda r: (parse_timestamp(r["created_at"]), r["id"]), reverse=True)
                page = rows[offset:offset + limit]
                return 200, {"requests": [self._request_obj(r) for r in page],
                             "has_more": offset + limit < len(rows)}

            if method == "GET" and path == "/activity":
                limit = page_value(query, "limit", 50, 1, 200)
                offset = page_value(query, "offset", 0, 0)
                rows = [p for p in self.state["payments"] if
                        p["visibility"] == "public" or uid in (p["from_user_id"], p["to_user_id"])]
                rows.sort(key=lambda p: (parse_timestamp(p["created_at"]), p["id"]), reverse=True)
                page = rows[offset:offset + limit]
                return 200, {"payments": [self._payment_obj(p) for p in page],
                             "has_more": offset + limit < len(rows)}

            if method == "GET" and path == "/statement":
                return 200, self._statement(uid, query, read_at)

            if method == "GET" and path == "/authorizations":
                direction = query.get("direction", [None])[0]
                status_filter = query.get("status", [None])[0]
                authorization_statuses = ("open", "captured", "voided", "expired")
                if direction not in (None, "incoming", "outgoing"):
                    fail(422, "validation_failed", "Invalid direction")
                if status_filter not in (None, *authorization_statuses):
                    fail(422, "validation_failed", "Invalid status")
                limit = page_value(query, "limit", 50, 1, 200)
                offset = page_value(query, "offset", 0, 0)
                rows = [a for a in self.state["authorizations"]
                        if uid in (a["from_user_id"], a["to_user_id"])]
                if direction == "outgoing":
                    rows = [a for a in rows if a["from_user_id"] == uid]
                elif direction == "incoming":
                    rows = [a for a in rows if a["to_user_id"] == uid]
                if status_filter is not None:
                    rows = [a for a in rows if a["status"] == status_filter]
                rows.sort(key=lambda a: (parse_timestamp(a["created_at"]), a["id"]), reverse=True)
                page = rows[offset:offset + limit]
                return 200, {"authorizations": [self._authorization_obj(a) for a in page],
                             "has_more": offset + limit < len(rows)}

            fail(404, "not_found", "Endpoint not found")


SERVICE = Service()
WEB_ROOT = Path(__file__).resolve().parent / "web"
UI_ROUTES = {"/", "/requests", "/split", "/assistant", "/paypal/return",
             "/signup", "/login", "/authorizations", "/analyst", "/admin", "/docs", "/commitments", "/activity"}


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):
        print("%s - %s" % (self.address_string(), fmt % args), flush=True)

    def _handle(self):
        try:
            length_raw = self.headers.get("Content-Length", "0")
            limit = (MAX_IMPORT_BODY_BYTES if urlsplit(self.path).path == "/_test/import"
                     else MAX_BODY_BYTES)
            # ASCII digits only, and no more of them than the limit has, so int() cannot fail.
            if (not (length_raw.isascii() and length_raw.isdigit())
                    or len(length_raw) > len(str(limit)) or int(length_raw) > limit):
                fail(400, "malformed_request", "Invalid request body length")
            raw_body = self.rfile.read(int(length_raw)) if int(length_raw) else b""
            parsed = urlsplit(self.path)
            accept = self.headers.get("Accept", "")
            if self.command in ("GET", "HEAD") and "text/html" in accept and parsed.path in UI_ROUTES:
                content = (WEB_ROOT / "index.html").read_bytes()
                self._send_bytes(200, content, "text/html; charset=utf-8")
                return
            if self.command in ("GET", "HEAD"):
                static_path = parsed.path
                # Handle /assets/ paths: keep the assets/ part in the path
                if static_path.startswith("/assets/"):
                    static_path = static_path[1:]  # Remove only the leading slash
                elif static_path.startswith("/"):
                    static_path = static_path[1:]
                # Serve analyst.html, admin.html and docs.html directly
                if static_path in ("analyst.html", "admin.html", "docs.html", "openapi.yaml"):
                    root = WEB_ROOT.resolve()
                    asset = (root / static_path).resolve()
                    if asset.is_relative_to(root) and asset.is_file():
                        content_type = "text/html"
                        self._send_bytes(200, asset.read_bytes(), content_type + "; charset=utf-8")
                        return
                if static_path and static_path not in ("index.html",):
                    root = WEB_ROOT.resolve()
                    asset = (root / static_path).resolve()
                    if asset.is_relative_to(root) and asset.is_file():
                        content_type = mimetypes.guess_type(asset.name)[0] or "application/octet-stream"
                        self._send_bytes(200, asset.read_bytes(), content_type + "; charset=utf-8")
                        return
            # HEAD is answered exactly like GET; _send_bytes omits the body.
            method = "GET" if self.command == "HEAD" else self.command
            request_headers = dict(self.headers.items())
            request_headers["_morrow_remote_address"] = self.client_address[0]
            status, payload = SERVICE.dispatch(method, self.path, request_headers, raw_body)
        except ApiError as exc:
            status = exc.status
            payload = {"error": {"code": exc.code, "message": exc.message}}
        except Exception:
            status = 500
            payload = {"error": {"code": "internal_error", "message": "Internal server error"}}
        if payload is None:
            encoded = b""
        else:
            encoded = json.dumps(payload, ensure_ascii=True, separators=(",", ":")).encode("utf-8")
        self._send_bytes(status, encoded, "application/json; charset=utf-8")

    def _send_bytes(self, status, encoded, content_type):
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(encoded)))
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        
        # CORS headers for cross-origin requests from frontend
        origin = self.headers.get("Origin", "")
        allowed_origins = [
            "https://morrow-frontend.onrender.com",
            "http://localhost:9000",  # local development
            "http://localhost:8080",  # local development
        ]
        
        if origin in allowed_origins:
            self.send_header("Access-Control-Allow-Origin", origin)
            self.send_header("Access-Control-Allow-Methods", "GET, POST, PUT, PATCH, DELETE, OPTIONS")
            self.send_header("Access-Control-Allow-Headers", "Content-Type, Authorization, Idempotency-Key")
            self.send_header("Access-Control-Max-Age", "86400")  # 24 hours
        
        if content_type.startswith("text/html"):
            self.send_header("Content-Security-Policy", CONTENT_SECURITY_POLICY)
        self.send_header("Connection", "close")
        self.end_headers()
        if encoded and self.command != "HEAD":
            self.wfile.write(encoded)
        self.close_connection = True

    do_GET = do_HEAD = do_POST = do_PUT = do_PATCH = do_DELETE = _handle

    def __getattr__(self, name):
        # Every other method (OPTIONS, TRACE, CONNECT, ...) gets the same JSON error handling
        # instead of http.server's HTML 501 page.
        if name.startswith("do_"):
            return self._handle
        raise AttributeError(name)

    def send_error(self, code, message=None, explain=None):
        """Unparseable request lines get the documented JSON error body, never HTML or a 5xx."""
        # Some Python versions reject the request line before recording its version, which
        # would suppress the status line and headers entirely.
        if self.request_version == "HTTP/0.9":
            self.request_version = self.protocol_version
        status = code if 400 <= code < 500 else 400
        error = "not_found" if status == 404 else "malformed_request"
        body = json.dumps({"error": {"code": error,
                                     "message": message or "Request could not be completed"}},
                          ensure_ascii=True, separators=(",", ":")).encode("utf-8")
        self._send_bytes(status, body, "application/json; charset=utf-8")


class Server(ThreadingHTTPServer):
    # The listen backlog must stay well above the 50 requests served in flight;
    # the default of 5 resets connections during a burst.
    request_queue_size = 128
    daemon_threads = True


def main():
    port = int(os.environ.get("PORT", "8080"))
    server = Server(("0.0.0.0", port), Handler)
    print(f"Morrow Stage 4 listening on 0.0.0.0:{port}", flush=True)
    server.serve_forever()


if __name__ == "__main__":
    main()
