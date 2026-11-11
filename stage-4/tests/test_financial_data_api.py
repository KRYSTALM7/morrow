"""Authenticated commitments, occurrences and shared-expense API contracts."""
import pytest

import fixtures as fx
from harness.http import assert_error, assert_status, new_key

pytestmark = pytest.mark.stage(4)


def commitment_body(**changes):
    body = {"title": "Monthly rent", "category": "housing", "amount_minor": 120000,
            "currency": "EUR", "frequency": "monthly", "next_due_date": "2027-01-31"}
    body.update(changes)
    return body


def shared_body(**changes):
    body = {"description": "Dinner", "category": "food", "gross_amount_minor": 1200,
            "personal_share_minor": 400, "currency": "EUR",
            "participants": [{"handle": "bob", "share_minor": 800}]}
    body.update(changes)
    return body


def test_commitment_create_list_update_and_occurrence_payment(reset, api):
    reset(fx.fixture())
    ada = api().authenticate(fx.ADA["email"], fx.ADA["password"])
    response = assert_status(ada.post("/commitments", json=commitment_body(),
                                     idempotency_key="commitment-create"), 201).json()
    duplicate = assert_status(ada.post("/commitments", json=commitment_body(),
                                       idempotency_key="commitment-create"), 200).json()
    assert duplicate["commitment_id"] == response["commitment_id"]
    commitment_id = response["commitment_id"]
    assert response["amount_minor"] == 120000
    assert response["next_due_date"] == "2027-01-31"
    assert response["status"] == "active"
    assert len(response["occurrences"]) == 12
    assert response["occurrences"][1]["due_date"] == "2027-02-28"

    listed = assert_status(ada.get("/commitments?status=active"), 200).json()
    assert [item["commitment_id"] for item in listed["commitments"]] == [commitment_id]
    occurrence = response["occurrences"][0]
    paid = assert_status(ada.post(
        f"/commitments/{commitment_id}/occurrences/{occurrence['occurrence_id']}/pay",
        json={"payment_reference": "bank-transfer-1"}, idempotency_key="occurrence-pay"), 201).json()
    assert paid["status"] == "paid"
    replay = assert_status(ada.post(
        f"/commitments/{commitment_id}/occurrences/{occurrence['occurrence_id']}/pay",
        json={"payment_reference": "bank-transfer-1"}, idempotency_key="occurrence-pay"), 200).json()
    assert replay == paid
    occurrences = assert_status(ada.get(f"/commitments/{commitment_id}/occurrences?status=unpaid"), 200).json()
    assert all(row["occurrence_id"] != occurrence["occurrence_id"] for row in occurrences["occurrences"])

    updated = assert_status(ada.request("PATCH", f"/commitments/{commitment_id}",
                                        json_body={"amount_minor": 125000},
                                        idempotency_key="commitment-update"), 201).json()
    assert updated["amount_minor"] == 125000
    assert next(row for row in updated["occurrences"]
                if row["occurrence_id"] == occurrence["occurrence_id"])["amount_minor"] == 120000
    assert all(row["amount_minor"] == 125000 for row in updated["occurrences"] if row["status"] == "unpaid")


def test_commitment_pause_resume_cancel_and_invalid_input(reset, api):
    reset(fx.fixture())
    ada = api().authenticate(fx.ADA["email"], fx.ADA["password"])
    created = assert_status(ada.post("/commitments", json=commitment_body(),
                                     idempotency_key="cm-create"), 201).json()
    commitment_id = created["commitment_id"]
    paused = assert_status(ada.post(f"/commitments/{commitment_id}/pause", json={},
                                    idempotency_key="cm-pause"), 201).json()
    assert paused["status"] == "paused" and paused["next_due_date"] is None
    resumed = assert_status(ada.post(f"/commitments/{commitment_id}/resume", json={},
                                     idempotency_key="cm-resume"), 201).json()
    assert resumed["status"] == "active"
    cancelled = assert_status(ada.post(f"/commitments/{commitment_id}/cancel", json={},
                                       idempotency_key="cm-cancel"), 201).json()
    assert cancelled["status"] == "cancelled"
    assert all(item["status"] in ("paid", "cancelled") for item in cancelled["occurrences"])

    assert_error(ada.post("/commitments", json=commitment_body(frequency="monthly", interval_days=7),
                          idempotency_key="bad-cadence"), 422, "validation_failed")
    assert_error(ada.post("/commitments", json=commitment_body(amount_minor=True),
                          idempotency_key="bad-amount"), 422, "validation_failed")
    assert_error(ada.post("/commitments", json=commitment_body(next_due_date="2027-02-30"),
                          idempotency_key="bad-date"), 422, "validation_failed")
    assert_error(ada.post("/commitments", json=commitment_body(unexpected="mass assignment"),
                          idempotency_key="bad-fields"), 422, "validation_failed")


def test_commitment_ownership_and_required_idempotency(reset, api):
    reset(fx.fixture())
    ada = api().authenticate(fx.ADA["email"], fx.ADA["password"])
    bob = api().authenticate(fx.BOB["email"], fx.BOB["password"])
    created = assert_status(ada.post("/commitments", json=commitment_body(),
                                     idempotency_key="owned-cm"), 201).json()
    assert_error(bob.get(f"/commitments/{created['commitment_id']}"), 404, "not_found")
    assert_error(bob.request("PATCH", f"/commitments/{created['commitment_id']}",
                             json_body={"title": "stolen"}, idempotency_key="stolen-edit"),
                 404, "not_found")
    assert_error(ada.post("/commitments", json=commitment_body()), 400, "missing_idempotency_key")


def test_shared_expense_partial_and_complete_reimbursement_history(reset, api):
    reset(fx.fixture())
    ada = api().authenticate(fx.ADA["email"], fx.ADA["password"])
    wallet_before = ada.get("/me").json()
    created = assert_status(ada.post("/shared-expenses", json=shared_body(),
                                     idempotency_key="expense-create"), 201).json()
    duplicate = assert_status(ada.post("/shared-expenses", json=shared_body(),
                                       idempotency_key="expense-create"), 200).json()
    assert duplicate["expense_id"] == created["expense_id"]
    expense_id = created["expense_id"]
    assert created["gross_amount_minor"] == 1200
    assert created["personal_share_minor"] == 400
    assert created["outstanding_receivable_minor"] == 800
    assert created["participants"][0]["outstanding_minor"] == 800
    assert ada.get("/me").json() == wallet_before

    partial_body = {"participant_handle": "bob", "amount_minor": 300,
                    "payment_reference": "cash-1"}
    partial = assert_status(ada.post(f"/shared-expenses/{expense_id}/settlements",
                                     json=partial_body, idempotency_key="partial-1"), 201).json()
    assert partial["settlement"]["source"] == "manual"
    assert partial["expense"]["outstanding_receivable_minor"] == 500
    assert ada.get("/me").json() == wallet_before
    duplicate = assert_status(ada.post(f"/shared-expenses/{expense_id}/settlements",
                                       json=partial_body, idempotency_key="partial-1"), 200).json()
    assert duplicate == partial
    assert len(ada.get(f"/shared-expenses/{expense_id}/settlements").json()["settlements"]) == 1

    complete = assert_status(ada.post(f"/shared-expenses/{expense_id}/settlements",
                                      json={"participant_handle": "bob", "amount_minor": 500},
                                      idempotency_key="partial-2"), 201).json()
    assert complete["expense"]["outstanding_receivable_minor"] == 0
    assert complete["expense"]["status"] == "settled"
    assert ada.get("/shared-expenses?status=settled").json()["expenses"][0]["expense_id"] == expense_id

    assert_error(ada.post(f"/shared-expenses/{expense_id}/settlements",
                          json={"participant_handle": "bob", "amount_minor": 1},
                          idempotency_key="overpay"), 409, "finance_conflict")
    assert_error(ada.post(f"/shared-expenses/{expense_id}/settlements",
                          json={"participant_handle": "bob", "amount_minor": 1, "source": "paypal"},
                          idempotency_key="paypal-not-linked"), 422, "validation_failed")


def test_shared_expense_validates_shares_currency_and_user_ownership(reset, api):
    reset(fx.fixture())
    ada = api().authenticate(fx.ADA["email"], fx.ADA["password"])
    bob = api().authenticate(fx.BOB["email"], fx.BOB["password"])
    assert_error(ada.post("/shared-expenses", json=shared_body(personal_share_minor=500),
                          idempotency_key="wrong-sum"), 422, "validation_failed")
    assert_error(ada.post("/shared-expenses", json=shared_body(currency="USD"),
                          idempotency_key="wrong-currency"), 422, "currency_mismatch")
    assert_error(ada.post("/shared-expenses", json=shared_body(participants=[
        {"handle": "bob", "share_minor": 400}, {"handle": "bob", "share_minor": 400}]),
        idempotency_key="duplicate-person"), 422, "validation_failed")
    created = assert_status(ada.post("/shared-expenses", json=shared_body(),
                                     idempotency_key="owner-expense"), 201).json()
    assert_error(bob.get(f"/shared-expenses/{created['expense_id']}"), 404, "not_found")
    assert_error(bob.post(f"/shared-expenses/{created['expense_id']}/settlements",
                          json={"participant_handle": "bob", "amount_minor": 100},
                          idempotency_key="cross-user"), 404, "not_found")
    assert_error(ada.post("/shared-expenses", json=shared_body()), 400, "missing_idempotency_key")
