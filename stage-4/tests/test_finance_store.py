"""Finance-store migrations, recurrence, settlement, idempotency and persistence contracts."""
from concurrent.futures import ThreadPoolExecutor

import pytest

from finance_store import (FinanceConflict, FinanceStore, FinanceValidation,
                           IdempotencyConflict)


@pytest.fixture
def store(tmp_path):
    return FinanceStore(str(tmp_path / "finance.sqlite3"))


def commitment_values(**updates):
    values = {"title": "Rent", "category": "housing", "amount_minor": 120000,
              "currency": "EUR", "frequency": "monthly", "interval_days": None,
              "next_due_date": "2027-01-31"}
    values.update(updates)
    return values


def shared_values():
    return {"description": "Shared dinner", "category": "food",
            "gross_amount_minor": 1200, "personal_share_minor": 400,
            "currency": "EUR", "participants": [
                {"user_id": "u_bob", "handle": "bob", "share_minor": 800}]}


def test_migration_and_monthly_occurrences_preserve_anchor_day(store):
    status, created = store.create_commitment("u_ada", commitment_values(), "create-1")
    assert status == 201
    assert len(created["occurrences"]) == 12
    assert [row["due_date"] for row in created["occurrences"][:3]] == [
        "2027-01-31", "2027-02-28", "2027-03-31"]
    reopened = FinanceStore(store.path)
    assert reopened.get_commitment("u_ada", created["commitment_id"])["next_due_date"] == "2027-01-31"
    with reopened._connect() as db:
        assert db.execute("SELECT MAX(version) FROM finance_schema_migrations").fetchone()[0] == 2


def test_paid_occurrence_is_never_unpaid_and_payment_is_idempotent(store):
    _, created = store.create_commitment("u_ada", commitment_values(), "create-2")
    occurrence = created["occurrences"][0]
    first_status, paid = store.pay_occurrence("u_ada", created["commitment_id"],
                                              occurrence["occurrence_id"], "bank-ref-1", "pay-1")
    replay_status, replay = store.pay_occurrence("u_ada", created["commitment_id"],
                                                 occurrence["occurrence_id"], "bank-ref-1", "pay-1")
    assert first_status == 201 and replay_status == 200 and replay == paid
    assert paid["status"] == "paid"
    assert paid["payment_reference"] == "bank-ref-1"
    unpaid = store.list_occurrences("u_ada", created["commitment_id"], status="unpaid")["occurrences"]
    assert all(row["occurrence_id"] != occurrence["occurrence_id"] for row in unpaid)
    with pytest.raises(FinanceConflict, match="not unpaid"):
        store.pay_occurrence("u_ada", created["commitment_id"], occurrence["occurrence_id"], None, "pay-2")


def test_commitment_update_pause_resume_cancel_and_paid_history(store):
    _, created = store.create_commitment("u_ada", commitment_values(), "create-3")
    commitment_id = created["commitment_id"]
    first = created["occurrences"][0]
    store.pay_occurrence("u_ada", commitment_id, first["occurrence_id"], None, "rent-paid")
    _, updated = store.update_commitment("u_ada", commitment_id,
                                         {"amount_minor": 130000}, "edit-1")
    rows = updated["occurrences"]
    assert next(row for row in rows if row["occurrence_id"] == first["occurrence_id"])["amount_minor"] == 120000
    assert all(row["amount_minor"] == 130000 for row in rows if row["status"] == "unpaid")
    _, paused = store.set_commitment_status("u_ada", commitment_id, "pause", "pause-1")
    assert paused["status"] == "paused" and paused["next_due_date"] is None
    _, resumed = store.set_commitment_status("u_ada", commitment_id, "resume", "resume-1")
    assert resumed["status"] == "active" and resumed["next_due_date"] is not None
    _, cancelled = store.set_commitment_status("u_ada", commitment_id, "cancel", "cancel-1")
    assert cancelled["status"] == "cancelled"
    assert all(row["status"] in ("paid", "cancelled") for row in cancelled["occurrences"])


def test_schedule_update_cancels_future_occurrences_without_erasing_history(store):
    _, created = store.create_commitment("u_ada", commitment_values(), "create-reschedule")
    commitment_id = created["commitment_id"]
    paid = created["occurrences"][0]
    store.pay_occurrence("u_ada", commitment_id, paid["occurrence_id"], None, "paid-before-reschedule")
    _, updated = store.update_commitment("u_ada", commitment_id, {
        "frequency": "weekly", "interval_days": None, "next_due_date": "2027-02-01"}, "reschedule")
    old = next(row for row in updated["occurrences"] if row["occurrence_id"] == paid["occurrence_id"])
    assert old["status"] == "paid"
    assert sum(row["status"] == "cancelled" for row in updated["occurrences"]) == 12
    assert sum(row["status"] == "unpaid" for row in updated["occurrences"]) == 12
    assert updated["next_due_date"] == "2027-02-01"
    with pytest.raises(FinanceValidation, match="after the last paid"):
        store.update_commitment("u_ada", commitment_id, {"next_due_date": "2027-01-01"}, "bad-reschedule")


def test_idempotency_key_cannot_be_reused_for_different_request(store):
    store.create_commitment("u_ada", commitment_values(), "same-key")
    with pytest.raises(IdempotencyConflict):
        store.create_commitment("u_ada", commitment_values(amount_minor=1), "same-key")


def test_partial_and_complete_reimbursements_have_append_only_history(store):
    _, expense = store.create_shared_expense("u_ada", shared_values(), "expense-1")
    expense_id = expense["expense_id"]
    assert expense["gross_amount_minor"] == 1200
    assert expense["personal_share_minor"] == 400
    assert expense["outstanding_receivable_minor"] == 800
    _, partial = store.settle_shared_expense("u_ada", expense_id, "u_bob", 300,
                                             "receipt-1", "settle-1")
    _, replay = store.settle_shared_expense("u_ada", expense_id, "u_bob", 300,
                                            "receipt-1", "settle-1")
    assert replay["settlement"] == partial["settlement"]
    assert partial["expense"]["outstanding_receivable_minor"] == 500
    assert partial["expense"]["participants"][0]["reimbursed_minor"] == 300
    with pytest.raises(FinanceConflict, match="exceeds"):
        store.settle_shared_expense("u_ada", expense_id, "u_bob", 501, None, "too-much")
    _, complete = store.settle_shared_expense("u_ada", expense_id, "u_bob", 500,
                                              None, "settle-2")
    assert complete["expense"]["outstanding_receivable_minor"] == 0
    assert complete["expense"]["status"] == "settled"
    assert len(store.list_settlements("u_ada", expense_id)["settlements"]) == 2
    with store._connect() as db:
        with pytest.raises(Exception, match="immutable"):
            db.execute("UPDATE shared_expense_settlements SET amount_minor=1")


def test_shared_expense_persists_and_is_owner_scoped(store):
    _, expense = store.create_shared_expense("u_ada", shared_values(), "expense-2")
    reopened = FinanceStore(store.path)
    assert reopened.get_shared_expense("u_ada", expense["expense_id"])["outstanding_receivable_minor"] == 800
    with pytest.raises(Exception, match="not found"):
        reopened.get_shared_expense("u_bob", expense["expense_id"])


def test_concurrent_same_key_settlement_is_applied_once(store):
    _, expense = store.create_shared_expense("u_ada", shared_values(), "expense-3")
    args = ("u_ada", expense["expense_id"], "u_bob", 300, None, "parallel-settle")
    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(lambda _: store.settle_shared_expense(*args), range(8)))
    assert sum(status == 201 for status, _ in results) == 1
    assert sum(status == 200 for status, _ in results) == 7
    detail = store.get_shared_expense("u_ada", expense["expense_id"])
    assert detail["total_reimbursed_minor"] == 300


def test_forecast_preferences_and_versioned_snapshots_persist(store):
    created_at = "2026-01-01T00:00:00+00:00"
    store.ensure_financial_preferences("u_ada", created_at)
    _, saved = store.update_financial_preferences(
        "u_ada", {"savings_target_minor": 5000, "safety_buffer_minor": 2500}, "targets-1")
    result = {"status": "unavailable", "model_version": "morrow-wallet-median-v1"}
    store.save_forecast_snapshot("u_ada", "2026-06-01", "2026-06-30", "2026-06-15",
                                 "morrow-wallet-median-v1", {"history_months": []}, result)
    reopened = FinanceStore(store.path)
    persisted = reopened.ensure_financial_preferences("u_ada")
    assert persisted["savings_target_minor"] == 5000
    assert persisted["safety_buffer_minor"] == 2500
    assert persisted["history_coverage_started_at"] == created_at
    assert saved["version"] == 2
    with reopened._connect() as db:
        snapshot = db.execute("SELECT model_version,inputs_json,result_json FROM forecast_snapshots").fetchone()
        assert snapshot["model_version"] == "morrow-wallet-median-v1"
        assert '"history_months":[]' in snapshot["inputs_json"]
        assert '"status":"unavailable"' in snapshot["result_json"]
