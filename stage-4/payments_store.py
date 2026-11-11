"""SQLite persistence for external PayPal checkout operations (never the social ledger)."""
from __future__ import annotations

import hashlib
import json
import sqlite3
import threading
from contextlib import closing
from datetime import datetime, timezone


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


class CheckoutStore:
    """Small SQLite store with versioned schema and append-only operation audit."""

    def __init__(self, path: str):
        self.path = path
        self._lock = threading.RLock()
        self._migrate()

    def _connect(self):
        connection = sqlite3.connect(self.path, timeout=10, isolation_level=None)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("PRAGMA busy_timeout=10000")
        return connection

    def _migrate(self):
        with self._lock, closing(self._connect()) as db:
            db.execute("PRAGMA journal_mode=WAL")
            db.execute("CREATE TABLE IF NOT EXISTS schema_migrations (version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL)")
            version = db.execute("SELECT COALESCE(MAX(version), 0) FROM schema_migrations").fetchone()[0]
            if version < 1:
                db.executescript("""
                    CREATE TABLE paypal_checkouts (
                      checkout_id TEXT PRIMARY KEY,
                      user_id TEXT NOT NULL,
                      idempotency_key TEXT NOT NULL,
                      request_hash TEXT NOT NULL,
                      request_json TEXT NOT NULL,
                      status TEXT NOT NULL,
                      order_id TEXT UNIQUE,
                      capture_id TEXT UNIQUE,
                      approval_url TEXT,
                      amount_minor INTEGER NOT NULL CHECK(amount_minor > 0),
                      currency TEXT NOT NULL CHECK(length(currency) = 3),
                      note TEXT NOT NULL,
                      provider_error TEXT,
                      created_at TEXT NOT NULL,
                      updated_at TEXT NOT NULL,
                      version INTEGER NOT NULL DEFAULT 1,
                      UNIQUE(user_id, idempotency_key)
                    );
                    CREATE INDEX paypal_checkouts_owner_created ON paypal_checkouts(user_id, created_at DESC);
                    CREATE TABLE paypal_webhook_events (
                      event_id TEXT PRIMARY KEY,
                      event_type TEXT NOT NULL,
                      order_id TEXT,
                      capture_id TEXT,
                      payload_hash TEXT NOT NULL,
                      verification_status TEXT NOT NULL,
                      processing_status TEXT NOT NULL,
                      received_at TEXT NOT NULL,
                      processed_at TEXT
                    );
                    CREATE TABLE paypal_checkout_audit (
                      sequence INTEGER PRIMARY KEY AUTOINCREMENT,
                      checkout_id TEXT NOT NULL REFERENCES paypal_checkouts(checkout_id),
                      from_status TEXT,
                      to_status TEXT NOT NULL,
                      event TEXT NOT NULL,
                      actor_user_id TEXT,
                      at TEXT NOT NULL,
                      details_json TEXT NOT NULL
                    );
                    CREATE TRIGGER paypal_checkout_audit_no_update
                      BEFORE UPDATE ON paypal_checkout_audit BEGIN SELECT RAISE(ABORT, 'audit rows are immutable'); END;
                    CREATE TRIGGER paypal_checkout_audit_no_delete
                      BEFORE DELETE ON paypal_checkout_audit BEGIN SELECT RAISE(ABORT, 'audit rows are immutable'); END;
                """)
                db.execute("INSERT INTO schema_migrations(version, applied_at) VALUES(1, ?)", (utc_now(),))

    @staticmethod
    def _row(row):
        return dict(row) if row else None

    def get(self, checkout_id: str):
        with self._lock, closing(self._connect()) as db:
            return self._row(db.execute("SELECT * FROM paypal_checkouts WHERE checkout_id=?",
                                        (checkout_id,)).fetchone())

    def get_for_key(self, user_id: str, key: str):
        with self._lock, closing(self._connect()) as db:
            return self._row(db.execute("SELECT * FROM paypal_checkouts WHERE user_id=? AND idempotency_key=?",
                                        (user_id, key)).fetchone())

    def get_for_order(self, order_id: str):
        with self._lock, closing(self._connect()) as db:
            return self._row(db.execute("SELECT * FROM paypal_checkouts WHERE order_id=?",
                                        (order_id,)).fetchone())

    def list_for_user(self, user_id: str, limit: int = 100):
        with self._lock, closing(self._connect()) as db:
            return [dict(row) for row in db.execute(
                "SELECT * FROM paypal_checkouts WHERE user_id=? ORDER BY created_at DESC LIMIT ?",
                (user_id, limit)).fetchall()]

    def claim(self, checkout_id: str, user_id: str, key: str, request: dict):
        request_json = json.dumps(request, sort_keys=True, separators=(",", ":"))
        request_hash = hashlib.sha256(request_json.encode()).hexdigest()
        now = utc_now()
        with self._lock, closing(self._connect()) as db:
            db.execute("BEGIN IMMEDIATE")
            try:
                old = db.execute("SELECT * FROM paypal_checkouts WHERE user_id=? AND idempotency_key=?",
                                 (user_id, key)).fetchone()
                if old:
                    if old["request_hash"] != request_hash:
                        db.execute("ROLLBACK")
                        return "conflict", dict(old)
                    db.execute("COMMIT")
                    return "existing", dict(old)
                db.execute("""INSERT INTO paypal_checkouts(checkout_id,user_id,idempotency_key,
                           request_hash,request_json,status,amount_minor,currency,note,created_at,updated_at)
                           VALUES(?,?,?,?,?,'CREATING',?,?,?,?,?)""",
                           (checkout_id, user_id, key, request_hash, request_json,
                            request["amount_minor"], request["currency"], request["note"], now, now))
                self._audit(db, checkout_id, None, "CREATING", "checkout_requested", user_id, {})
                row = db.execute("SELECT * FROM paypal_checkouts WHERE checkout_id=?", (checkout_id,)).fetchone()
                db.execute("COMMIT")
                return "created", dict(row)
            except Exception:
                db.execute("ROLLBACK")
                raise

    @staticmethod
    def _audit(db, checkout_id, before, after, event, actor, details):
        db.execute("INSERT INTO paypal_checkout_audit(checkout_id,from_status,to_status,event,actor_user_id,at,details_json) VALUES(?,?,?,?,?,?,?)",
                   (checkout_id, before, after, event, actor, utc_now(),
                    json.dumps(details, sort_keys=True, separators=(",", ":"))))

    def transition(self, checkout_id: str, status: str, *, event: str, actor: str | None = None,
                   order_id: str | None = None, capture_id: str | None = None,
                   approval_url: str | None = None, provider_error: str | None = None,
                   details: dict | None = None):
        with self._lock, closing(self._connect()) as db:
            db.execute("BEGIN IMMEDIATE")
            try:
                row = db.execute("SELECT * FROM paypal_checkouts WHERE checkout_id=?", (checkout_id,)).fetchone()
                if row is None:
                    db.execute("ROLLBACK")
                    return None
                before = row["status"]
                ranks = {"CREATING": 0, "CREATE_UNKNOWN": 0, "CREATED": 1,
                         "APPROVED": 2, "CAPTURE_IN_PROGRESS": 3, "CAPTURE_UNKNOWN": 3,
                         "CAPTURE_PENDING": 4, "FAILED": 4, "VOIDED": 4,
                         "COMPLETED": 5, "REVIEW_REQUIRED": 5,
                         "PARTIALLY_REFUNDED": 6, "REFUNDED": 6, "REVERSED": 6}
                # A later notification cannot regress an observed capture or terminal order.
                selected = status if ranks.get(status, 0) >= ranks.get(before, 0) else before
                new_order = order_id or row["order_id"]
                new_capture = capture_id or row["capture_id"]
                new_approval = approval_url or row["approval_url"]
                error = provider_error if selected == status else row["provider_error"]
                db.execute("""UPDATE paypal_checkouts SET status=?, order_id=?, capture_id=?, approval_url=?,
                           provider_error=?, updated_at=?, version=version+1 WHERE checkout_id=?""",
                           (selected, new_order, new_capture, new_approval, error, utc_now(), checkout_id))
                self._audit(db, checkout_id, before, selected, event, actor, details or {})
                new_row = db.execute("SELECT * FROM paypal_checkouts WHERE checkout_id=?", (checkout_id,)).fetchone()
                db.execute("COMMIT")
                return dict(new_row)
            except Exception:
                db.execute("ROLLBACK")
                raise

    def record_webhook(self, *, event_id: str, event_type: str, order_id: str | None,
                       capture_id: str | None, payload_hash: str):
        with self._lock, closing(self._connect()) as db:
            cursor = db.execute("""INSERT OR IGNORE INTO paypal_webhook_events
                (event_id,event_type,order_id,capture_id,payload_hash,verification_status,processing_status,received_at)
                VALUES(?,?,?,?,?,'VERIFIED','PENDING',?)""",
                (event_id, event_type, order_id, capture_id, payload_hash, utc_now()))
            row = db.execute("SELECT * FROM paypal_webhook_events WHERE event_id=?", (event_id,)).fetchone()
            return cursor.rowcount == 1, dict(row)

    def finish_webhook(self, event_id: str, status: str):
        with self._lock, closing(self._connect()) as db:
            db.execute("UPDATE paypal_webhook_events SET processing_status=?,processed_at=? WHERE event_id=?",
                       (status, utc_now(), event_id))

    def audit(self, checkout_id: str):
        with self._lock, closing(self._connect()) as db:
            return [dict(row) for row in db.execute(
                "SELECT * FROM paypal_checkout_audit WHERE checkout_id=? ORDER BY sequence",
                (checkout_id,)).fetchall()]

