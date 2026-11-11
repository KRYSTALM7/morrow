"""Authenticated forecasting and safe-to-spend API integration contracts."""
import calendar
from datetime import date, datetime, timezone

import pytest

import fixtures as fx
from harness.http import assert_error, assert_status, new_key

pytestmark = pytest.mark.stage(4)


def shift_month(value, offset):
    ordinal = value.year * 12 + value.month - 1 + offset
    year, month0 = divmod(ordinal, 12)
    month = month0 + 1
    return date(year, month, min(value.day, calendar.monthrange(year, month)[1]))


def historical_fixture():
    now = datetime.now(timezone.utc)
    month_start = now.date().replace(day=1)
    history_start = shift_month(month_start, -3)
    payments = []
    for offset, amount in ((-3, 1000), (-2, 2000), (-1, 3000)):
        period = shift_month(month_start, offset)
        payments.append({"id": f"forecast-{offset}", "from_user_id": "u_ada",
                         "to_user_id": "u_bob", "amount": amount,
                         "note": "wallet outflow", "visibility": "private",
                         "created_at": datetime(period.year, period.month, 12, 12,
                                                tzinfo=timezone.utc).isoformat()})
    users = [dict(fx.ADA), {**fx.BOB, "balance": 10000}, dict(fx.CY)]
    fixture = fx.fixture(users=users, payments=payments)
    fixture["forecast_coverage_started_at"] = {
        "u_ada": datetime(history_start.year, history_start.month, history_start.day,
                           tzinfo=timezone.utc).isoformat()}
    return fixture


def test_forecast_and_safe_to_spend_use_wallet_available_once(reset, api):
    reset(historical_fixture())
    ada, bob = (api().authenticate(user["email"], user["password"])
                for user in (fx.ADA, fx.BOB))
    settings = assert_status(ada.request(
        "PATCH", "/forecast/settings",
        json_body={"savings_target_minor": 1000, "safety_buffer_minor": 200},
        idempotency_key="forecast-preferences"), 200).json()
    assert settings["savings_target_minor"] == 1000
    assert settings["safety_buffer_minor"] == 200

    commitment = assert_status(ada.post("/commitments", json={
        "title": "Rent", "category": "housing", "amount_minor": 800,
        "currency": "EUR", "frequency": "monthly",
        "next_due_date": date.today().replace(day=1).isoformat()},
        idempotency_key="forecast-rent"), 201).json()
    assert_status(ada.post("/authorizations", json={
        "to_handle": "bob", "amount": 2500}, idempotency_key="forecast-hold"), 201)
    assert_status(ada.post("/shared-expenses", json={
        "description": "Shared meal", "category": "food",
        "gross_amount_minor": 1000, "personal_share_minor": 400,
        "currency": "EUR", "participants": [{"handle": "bob", "share_minor": 600}]},
        idempotency_key="forecast-expense"), 201)

    forecast = assert_status(ada.get("/forecast"), 200).json()
    safe = assert_status(ada.get("/safe-to-spend"), 200).json()
    assert forecast == safe
    assert forecast["status"] == "available"
    assert forecast["available_funds_minor"] == 7500  # balance less open hold, once
    assert forecast["unpaid_commitments_minor"] == 800
    assert forecast["projected_monthly_variable_spend_minor"] == 2000
    assert forecast["forecast_remaining_variable_spend_minor"] == 2000
    assert forecast["safe_to_spend_minor"] == 3500
    assert forecast["data_coverage"]["history_months"] == 3
    assert forecast["data_coverage"]["variable_category_status"] == "unavailable"
    assert forecast["variable_category_breakdown"] == []
    assert "holds are deducted once" in " ".join(forecast["assumptions"])
    assert bob.get("/forecast").json()["available_funds_minor"] == 10000

    occurrence = next(item for item in commitment["occurrences"] if item["status"] == "unpaid")
    assert_status(ada.post(
        f"/commitments/{commitment['commitment_id']}/occurrences/{occurrence['occurrence_id']}/pay",
        json={"payment_reference": "forecast--2"},
        idempotency_key="forecast-paid-rent"), 201)
    after_payment = assert_status(ada.get("/safe-to-spend"), 200).json()
    assert after_payment["unpaid_commitments_minor"] == 0
    assert after_payment["projected_monthly_variable_spend_minor"] == 1000
    assert after_payment["available_funds_minor"] == 7500


def test_forecast_returns_unavailable_when_history_coverage_is_short(reset, api):
    reset(fx.fixture())
    ada = api().authenticate(fx.ADA["email"], fx.ADA["password"])
    forecast = assert_status(ada.get("/forecast"), 200).json()
    assert forecast["status"] == "unavailable"
    assert forecast["safe_to_spend_minor"] is None
    assert "insufficient_complete_history" in forecast["unavailable_reasons"]
    assert forecast["data_coverage"]["complete"] is False


def test_settings_are_persistent_idempotent_and_user_scoped(reset, api):
    reset(fx.fixture())
    ada = api().authenticate(fx.ADA["email"], fx.ADA["password"])
    bob = api().authenticate(fx.BOB["email"], fx.BOB["password"])
    body = {"savings_target_minor": 1250}
    first = assert_status(ada.request("PATCH", "/forecast/settings", json_body=body,
                                      idempotency_key="save-target"), 200).json()
    replay = assert_status(ada.request("PATCH", "/forecast/settings", json_body=body,
                                       idempotency_key="save-target"), 200).json()
    assert first == replay
    assert ada.get("/forecast/settings").json()["savings_target_minor"] == 1250
    assert bob.get("/forecast/settings").json()["savings_target_minor"] == 0
    assert_error(ada.request("PATCH", "/forecast/settings",
                             json_body={"savings_target_minor": True},
                             idempotency_key="bad-target"), 422, "validation_failed")
    assert_error(ada.request("PATCH", "/forecast/settings",
                             json_body={"unsafe_user_id": "u_bob"},
                             idempotency_key="bad-fields"), 422, "validation_failed")
    assert_error(api().get("/safe-to-spend"), 401, "unauthenticated")


def test_unreceived_shared_expense_receivable_never_changes_available_cash(reset, api):
    reset(fx.fixture())
    ada = api().authenticate(fx.ADA["email"], fx.ADA["password"])
    before = ada.get("/me").json()["available"]
    expense = assert_status(ada.post("/shared-expenses", json={
        "description": "Shared meal", "category": "food",
        "gross_amount_minor": 1000, "personal_share_minor": 400,
        "currency": "EUR", "participants": [{"handle": "bob", "share_minor": 600}]},
        idempotency_key="receivable-test"), 201).json()
    assert expense["outstanding_receivable_minor"] == 600
    assert ada.get("/safe-to-spend").json()["available_funds_minor"] == before
    assert_status(ada.post(f"/shared-expenses/{expense['expense_id']}/settlements", json={
        "participant_handle": "bob", "amount_minor": 300},
        idempotency_key="record-reimbursement"), 201)
    assert ada.get("/safe-to-spend").json()["available_funds_minor"] == before
