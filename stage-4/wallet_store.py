"""Transactional SQLite persistence for the Stage 4 wallet state snapshot."""
from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import threading
from contextlib import closing, contextmanager
from datetime import datetime, timedelta, timezone


def utc_now():
    return datetime.now(timezone.utc).isoformat()


class WalletStore:
    """Versioned canonical wallet snapshot; each dispatch is serialized by SQLite."""

    def __init__(self, path):
        self.path = os.path.abspath(path)
        self._lock = threading.RLock()
        self._local = threading.local()
        os.makedirs(os.path.dirname(self.path) or ".", exist_ok=True)
        self._migrate()

    def _connect(self):
        db = sqlite3.connect(self.path, timeout=15, isolation_level=None)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA foreign_keys=ON")
        db.execute("PRAGMA busy_timeout=15000")
        db.execute("PRAGMA secure_delete=ON")
        return db

    def _migrate(self):
        with self._lock, closing(self._connect()) as db:
            db.execute("PRAGMA journal_mode=WAL")
            db.execute("CREATE TABLE IF NOT EXISTS wallet_schema_migrations "
                       "(version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL)")
            version = db.execute("SELECT COALESCE(MAX(version),0) FROM wallet_schema_migrations").fetchone()[0]
            if version < 1:
                db.execute("BEGIN IMMEDIATE")
                try:
                    db.execute("""CREATE TABLE wallet_state (
                        singleton INTEGER PRIMARY KEY CHECK(singleton=1),
                        state_version INTEGER NOT NULL,
                        state_json TEXT NOT NULL,
                        updated_at TEXT NOT NULL
                    )""")
                    db.execute("""CREATE TABLE wallet_auth_attempts (
                        bucket_hash TEXT PRIMARY KEY,
                        window_started REAL NOT NULL,
                        attempts INTEGER NOT NULL CHECK(attempts >= 0),
                        updated_at TEXT NOT NULL
                    )""")
                    db.execute("INSERT INTO wallet_schema_migrations(version,applied_at) VALUES(1,?)",
                               (utc_now(),))
                    db.execute("COMMIT")
                except Exception:
                    db.execute("ROLLBACK")
                    raise
                version = 1
            if version < 2:
                db.executescript("""
                    CREATE TABLE wallet_sessions (
                      token_hash TEXT PRIMARY KEY,
                      user_id TEXT NOT NULL,
                      created_at TEXT NOT NULL,
                      expires_at TEXT NOT NULL,
                      revoked_at TEXT
                    );
                    CREATE INDEX wallet_sessions_owner_expiry ON wallet_sessions(user_id,expires_at);
                """)
                db.execute("INSERT INTO wallet_schema_migrations(version,applied_at) VALUES(2,?)",
                           (utc_now(),))
                version = 2
            if version < 3:
                # Migrate the legacy token map into hash-only sessions and scrub the
                # old JSON value in the same transaction. Existing session rows win,
                # preserving their expiry/revocation state.
                db.execute("BEGIN IMMEDIATE")
                try:
                    current_version = db.execute(
                        "SELECT COALESCE(MAX(version),0) FROM wallet_schema_migrations").fetchone()[0]
                    if current_version < 3:
                        row = db.execute("SELECT state_json FROM wallet_state WHERE singleton=1").fetchone()
                        if row:
                            state = json.loads(row["state_json"])
                            tokens = state.pop("tokens", {})
                            current = datetime.now(timezone.utc)
                            expiry = (current + timedelta(days=30)).isoformat()
                            for token, user_id in tokens.items():
                                db.execute("""INSERT OR IGNORE INTO wallet_sessions
                                    (token_hash,user_id,created_at,expires_at) VALUES(?,?,?,?)""",
                                           (self.token_hash(token), user_id, current.isoformat(), expiry))
                            db.execute("UPDATE wallet_state SET state_json=?,updated_at=? WHERE singleton=1",
                                       (self._encode(state), utc_now()))
                        db.execute("INSERT INTO wallet_schema_migrations(version,applied_at) VALUES(3,?)",
                                   (utc_now(),))
                    db.execute("COMMIT")
                except Exception:
                    if db.in_transaction:
                        db.execute("ROLLBACK")
                    raise
                version = max(version, 3)
            if version < 4:
                # Keep a separate completion marker so an interrupted scrub is
                # retried at startup before any wallet session is served.
                if db.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()[0]:
                    raise RuntimeError("Could not checkpoint wallet WAL during token migration")
                db.execute("VACUUM")
                if db.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()[0]:
                    raise RuntimeError("Could not truncate wallet WAL after token migration")
                db.execute("BEGIN IMMEDIATE")
                try:
                    current_version = db.execute(
                        "SELECT COALESCE(MAX(version),0) FROM wallet_schema_migrations").fetchone()[0]
                    if current_version < 4:
                        db.execute("INSERT INTO wallet_schema_migrations(version,applied_at) VALUES(4,?)",
                                   (utc_now(),))
                    db.execute("COMMIT")
                except Exception:
                    if db.in_transaction:
                        db.execute("ROLLBACK")
                    raise
        try:
            os.chmod(self.path, 0o600)
        except OSError:
            pass

    @staticmethod
    def _encode(state):
        persisted = dict(state)
        persisted.pop("tokens", None)
        return json.dumps(persisted, sort_keys=True, separators=(",", ":"), ensure_ascii=True)

    @staticmethod
    def _decode(row):
        if not row:
            return None
        state = json.loads(row["state_json"])
        state["tokens"] = {}
        return state

    def load(self):
        with self._lock, closing(self._connect()) as db:
            row = db.execute("SELECT state_json FROM wallet_state WHERE singleton=1").fetchone()
            return self._decode(row)

    def initialize(self, state):
        with self._lock, closing(self._connect()) as db:
            db.execute("BEGIN IMMEDIATE")
            try:
                row = db.execute("SELECT 1 FROM wallet_state WHERE singleton=1").fetchone()
                if row:
                    db.execute("ROLLBACK")
                    return False
                db.execute("INSERT INTO wallet_state(singleton,state_version,state_json,updated_at) VALUES(1,1,?,?)",
                           (self._encode(state), utc_now()))
                for token, user_id in state.get("tokens", {}).items():
                    db.execute("INSERT INTO wallet_sessions(token_hash,user_id,created_at,expires_at,revoked_at) VALUES(?,?,?,?,NULL)",
                               (self.token_hash(token), user_id, utc_now(),
                                (datetime.now(timezone.utc) + timedelta(days=30)).isoformat()))
                db.execute("COMMIT")
                return True
            except Exception:
                if db.in_transaction:
                    db.execute("ROLLBACK")
                raise

    @contextmanager
    def transaction(self):
        """Yield fresh state under a cross-process write lock; caller commits via save()."""
        with self._lock, closing(self._connect()) as db:
            db.execute("BEGIN IMMEDIATE")
            self._local.db = db
            try:
                row = db.execute("SELECT state_json FROM wallet_state WHERE singleton=1").fetchone()
                if row is None:
                    raise RuntimeError("Wallet state was not initialized")
                yield self._decode(row)
                db.execute("COMMIT")
            except Exception:
                if db.in_transaction:
                    db.execute("ROLLBACK")
                raise
            finally:
                self._local.db = None

    def save(self, state):
        db = getattr(self._local, "db", None)
        if db is None or not db.in_transaction:
            raise RuntimeError("Wallet state writes require an active transaction")
        db.execute("UPDATE wallet_state SET state_version=state_version+1,state_json=?,updated_at=? WHERE singleton=1",
                   (self._encode(state), utc_now()))
    def create_session(self, token, user_id, *, expires_at):
        db = getattr(self._local, "db", None)
        if db is None or not db.in_transaction:
            raise RuntimeError("Session writes require an active transaction")
        db.execute("INSERT INTO wallet_sessions(token_hash,user_id,created_at,expires_at,revoked_at) VALUES(?,?,?,?,NULL)",
                   (self.token_hash(token), user_id, utc_now(), expires_at))

    def revoke_session(self, token, *, now=None):
        db = getattr(self._local, "db", None)
        if db is None or not db.in_transaction:
            raise RuntimeError("Session writes require an active transaction")
        db.execute("UPDATE wallet_sessions SET revoked_at=COALESCE(revoked_at,?) WHERE token_hash=?",
                   (now or utc_now(), self.token_hash(token)))

    @staticmethod
    def token_hash(token):
        return hashlib.sha256(token.encode("utf-8")).hexdigest()

    def session(self, token, *, now=None):
        db = getattr(self._local, "db", None)
        if db is None or not db.in_transaction:
            with self._lock, closing(self._connect()) as read_db:
                row = read_db.execute("SELECT user_id,expires_at,revoked_at FROM wallet_sessions WHERE token_hash=?",
                                      (self.token_hash(token),)).fetchone()
        else:
            row = db.execute("SELECT user_id,expires_at,revoked_at FROM wallet_sessions WHERE token_hash=?",
                             (self.token_hash(token),)).fetchone()
        if row is None:
            return None
        if row["revoked_at"] is not None or row["expires_at"] <= (now or utc_now()):
            return {"user_id": None, "expired": row["revoked_at"] is None}
        return {"user_id": row["user_id"], "expired": False}

    @staticmethod
    def _attempt_key(kind, identifier):
        return hashlib.sha256(f"morrow-auth:{kind}:{identifier}".encode()).hexdigest()

    def auth_attempts(self, kind, identifier, *, now, window_seconds=900):
        db = getattr(self._local, "db", None)
        if db is None or not db.in_transaction:
            raise RuntimeError("Authentication throttling requires a wallet transaction")
        key = self._attempt_key(kind, identifier)
        row = db.execute("SELECT window_started,attempts FROM wallet_auth_attempts WHERE bucket_hash=?",
                         (key,)).fetchone()
        if row is None or now - row["window_started"] >= window_seconds:
            return 0
        return row["attempts"]

    def record_auth_attempt(self, kind, identifier, *, now, window_seconds=900):
        db = getattr(self._local, "db", None)
        if db is None or not db.in_transaction:
            raise RuntimeError("Authentication throttling requires a wallet transaction")
        key = self._attempt_key(kind, identifier)
        row = db.execute("SELECT window_started,attempts FROM wallet_auth_attempts WHERE bucket_hash=?",
                         (key,)).fetchone()
        if row is None or now - row["window_started"] >= window_seconds:
            db.execute("INSERT OR REPLACE INTO wallet_auth_attempts VALUES(?,?,1,?)",
                       (key, now, utc_now()))
        else:
            db.execute("UPDATE wallet_auth_attempts SET attempts=attempts+1,updated_at=? WHERE bucket_hash=?",
                       (utc_now(), key))
        db.execute("DELETE FROM wallet_auth_attempts WHERE window_started < ?",
                   (now - window_seconds * 2,))

    def clear_auth_attempts(self, kind, identifier):
        db = getattr(self._local, "db", None)
        if db is None or not db.in_transaction:
            raise RuntimeError("Authentication throttling requires a wallet transaction")
        db.execute("DELETE FROM wallet_auth_attempts WHERE bucket_hash=?",
                   (self._attempt_key(kind, identifier),))

    def reset_auth_attempts_for_tests(self):
        db = getattr(self._local, "db", None)
        if db is not None and db.in_transaction:
            db.execute("DELETE FROM wallet_auth_attempts")
            return
        with self._lock, closing(self._connect()) as connection:
            connection.execute("DELETE FROM wallet_auth_attempts")

    def health(self):
        with self._lock, closing(self._connect()) as db:
            integrity = db.execute("PRAGMA quick_check").fetchone()[0]
            version = db.execute("SELECT COALESCE(MAX(version),0) FROM wallet_schema_migrations").fetchone()[0]
            initialized = db.execute("SELECT 1 FROM wallet_state WHERE singleton=1").fetchone() is not None
            return {"status": "ok" if integrity == "ok" and version >= 1 and initialized else "error",
                    "migration_version": version, "initialized": initialized,
                    "integrity": integrity}
