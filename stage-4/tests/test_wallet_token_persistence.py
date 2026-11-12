"""Bearer credentials are never written into wallet snapshots or SQLite pages."""
import hashlib
import json
import sqlite3
from types import SimpleNamespace

from app import Service
from wallet_store import WalletStore


def test_wallet_snapshot_and_session_rows_store_only_token_hash(tmp_path):
    db_path = tmp_path / "wallet.sqlite3"
    store = WalletStore(str(db_path))
    raw = "known-raw-bearer-token-must-never-appear"
    state = {"users": {"u_one": {"id": "u_one", "balance": 1234}},
             "tokens": {raw: "u_one"}, "payments": [], "authorizations": []}
    store.initialize(state)

    encoded = WalletStore._encode(state)
    assert raw not in encoded
    assert '"tokens"' not in encoded
    assert raw not in Service._serialize_state(SimpleNamespace(state=state))
    with sqlite3.connect(db_path) as db:
        state_json = db.execute("SELECT state_json FROM wallet_state").fetchone()[0]
        sessions = db.execute("SELECT token_hash,user_id FROM wallet_sessions").fetchall()
    assert raw not in state_json
    assert '"tokens"' not in state_json
    assert sessions == [(hashlib.sha256(raw.encode()).hexdigest(), "u_one")]
    assert raw not in db_path.read_bytes().decode("latin1")


def test_legacy_plaintext_snapshot_migrates_without_losing_wallet_data(tmp_path):
    db_path = tmp_path / "legacy-wallet.sqlite3"
    store = WalletStore(str(db_path))
    raw = "legacy-live-bearer-token"
    revoked_raw = "legacy-revoked-bearer-token"
    state = {"currency": "EUR", "minor_units": 2,
             "users": {"u_ada": {"id": "u_ada", "balance": 9876}},
             "tokens": {}, "payments": [{"id": "pay-1", "amount": 124}],
             "authorizations": [{"id": "hold-1", "amount": 300}],
             "finance_marker": "preserve-me"}
    store.initialize(state)
    # Simulate a version 2 database left by the vulnerable release.
    legacy = dict(state)
    legacy["tokens"] = {raw: "u_ada", revoked_raw: "u_ada"}
    with sqlite3.connect(db_path) as db:
        db.execute("UPDATE wallet_state SET state_json=? WHERE singleton=1",
                   (json.dumps(legacy, sort_keys=True, separators=(",", ":")),))
        db.execute("DELETE FROM wallet_schema_migrations WHERE version>=3")
        db.execute("DELETE FROM wallet_sessions")
        db.execute("INSERT INTO wallet_sessions VALUES(?,?,?,?,NULL)",
                   (hashlib.sha256(raw.encode()).hexdigest(), "u_ada",
                    "2026-01-01T00:00:00+00:00", "2099-01-01T00:00:00+00:00"))
        db.execute("INSERT INTO wallet_sessions VALUES(?,?,?,?,?)",
                   (hashlib.sha256(revoked_raw.encode()).hexdigest(), "u_ada",
                    "2026-01-01T00:00:00+00:00", "2099-01-01T00:00:00+00:00",
                    "2026-02-01T00:00:00+00:00"))

    migrated = WalletStore(str(db_path))
    restored = migrated.load()
    assert restored["users"]["u_ada"]["balance"] == 9876
    assert restored["payments"] == state["payments"]
    assert restored["authorizations"] == state["authorizations"]
    assert restored["finance_marker"] == "preserve-me"
    assert migrated.session(raw)["user_id"] == "u_ada"
    assert migrated.session(revoked_raw)["user_id"] is None
    with sqlite3.connect(db_path) as db:
        state_json = db.execute("SELECT state_json FROM wallet_state").fetchone()[0]
        token_hash, expires_at = db.execute(
            "SELECT token_hash,expires_at FROM wallet_sessions WHERE token_hash=?",
            (hashlib.sha256(raw.encode()).hexdigest(),)).fetchone()
        version = db.execute("SELECT MAX(version) FROM wallet_schema_migrations").fetchone()[0]
    assert raw not in state_json
    assert '"tokens"' not in state_json
    assert token_hash == hashlib.sha256(raw.encode()).hexdigest()
    assert expires_at == "2099-01-01T00:00:00+00:00"
    assert version == 6  # Current latest migration version
    # The application has no backup writer; also ensure the live database and SQLite
    # sidecars contain no legacy token text after migration/checkpoint/vacuum.
    for path in (db_path, tmp_path / "legacy-wallet.sqlite3-wal",
                 tmp_path / "legacy-wallet.sqlite3-shm"):
        if path.exists():
            assert raw.encode() not in path.read_bytes()
            assert revoked_raw.encode() not in path.read_bytes()
