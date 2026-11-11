"""Wallet snapshots and bearer sessions survive a process restart."""
import os
import hashlib
import json
import sqlite3
import socket
import subprocess
import sys
import time
from pathlib import Path

from harness.http import Api, assert_status
import fixtures as fx

ROOT = Path(__file__).resolve().parents[1]


def free_port():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


class ServiceProcess:
    def __init__(self, env):
        self.port = free_port()
        self.env = {**os.environ, **env, "PORT": str(self.port),
                    "POCKETFUL_ENABLE_TEST_ROUTES": "1"}
        self.process = None

    @property
    def api(self):
        return Api(f"http://127.0.0.1:{self.port}")

    def start(self):
        self.process = subprocess.Popen([sys.executable, str(ROOT / "app.py")],
                                        cwd=ROOT, env=self.env,
                                        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            if self.process.poll() is not None:
                raise RuntimeError("Stage 4 service exited during startup")
            try:
                if self.api.get("/health").status_code == 200:
                    return self
            except Exception:
                time.sleep(0.05)
        self.stop()
        raise RuntimeError("Stage 4 service did not become healthy")

    def stop(self):
        if self.process is None:
            return
        self.process.terminate()
        try:
            self.process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            self.process.kill()
            self.process.wait()
        self.process = None


def test_wallet_and_session_recover_after_application_restart(tmp_path):
    wallet_db = tmp_path / "wallet.sqlite3"
    finance_db = tmp_path / "finance.sqlite3"
    paypal_db = tmp_path / "paypal.sqlite3"
    env = {"MORROW_WALLET_DB_PATH": str(wallet_db),
           "MORROW_FINANCE_DB_PATH": str(finance_db),
           "PAYPAL_DB_PATH": str(paypal_db)}
    service = ServiceProcess(env).start()
    try:
        assert_status(service.api.post("/_test/reset", json=fx.fixture()), 204)
        ada = service.api.authenticate(fx.ADA["email"], fx.ADA["password"])
        bob = service.api.authenticate(fx.BOB["email"], fx.BOB["password"])
        with sqlite3.connect(wallet_db) as db:
            state_json = db.execute("SELECT state_json FROM wallet_state").fetchone()[0]
            session_hashes = {row[0] for row in db.execute(
                "SELECT token_hash FROM wallet_sessions WHERE revoked_at IS NULL")}
        assert "tokens" not in json.loads(state_json)
        assert ada.token not in state_json and bob.token not in state_json
        assert hashlib.sha256(ada.token.encode()).hexdigest() in session_hashes
        assert hashlib.sha256(bob.token.encode()).hexdigest() in session_hashes
        payment_body = {"to_handle": "bob", "amount": 725, "note": "durable transfer"}
        created = assert_status(ada.post("/payments", json=payment_body,
                                         idempotency_key="restart-payment"), 201).json()
        hold_body = {"to_handle": "cy", "amount": 400, "note": "durable hold"}
        hold = assert_status(ada.post("/authorizations", json=hold_body,
                                      idempotency_key="restart-hold"), 201).json()
        assert_status(bob.post("/auth/logout"), 200)
        with sqlite3.connect(wallet_db) as db:
            bob_revoked = db.execute("SELECT revoked_at FROM wallet_sessions WHERE token_hash=?",
                                     (hashlib.sha256(bob.token.encode()).hexdigest(),)).fetchone()[0]
        assert bob_revoked is not None
        before = ada.get("/me").json()
        assert before["balance"] == fx.ADA["balance"] - 725, (created, before)
        assert before["held"] == 400
    finally:
        service.stop()

    service = ServiceProcess(env).start()
    try:
        ada_after_restart = Api(service.api.base_url, ada.token)
        bob_after_restart = Api(service.api.base_url, bob.token)
        recovered = assert_status(ada_after_restart.get("/me"), 200).json()
        assert recovered["balance"] == before["balance"]
        assert recovered["held"] == before["held"]
        activity = assert_status(ada_after_restart.get("/activity"), 200).json()
        assert any(row["payment_id"] == created["payment_id"] for row in activity["payments"])
        authorizations = assert_status(ada_after_restart.get("/authorizations"), 200).json()
        assert any(row["authorization_id"] == hold["authorization_id"]
                   for row in authorizations["authorizations"])
        assert bob_after_restart.get("/me").status_code == 401
        replay = assert_status(ada_after_restart.post("/payments", json=payment_body,
                                                       idempotency_key="restart-payment"), 200).json()
        assert replay["payment_id"] == created["payment_id"]
        assert assert_status(ada_after_restart.get("/me"), 200).json()["balance"] == before["balance"]
        assert service.api.get("/me").status_code == 401
    finally:
        service.stop()
