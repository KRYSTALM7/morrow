"""Pure deterministic forecast and safe-to-spend calculation contracts."""
from datetime import date

import pytest

from forecast_engine import calculate_forecast

pytestmark = pytest.mark.stage(4)


def entry(payment_id, amount, delta, effective_at, **payment_fields):
    payment = {"payment_id": payment_id, "amount": amount, **payment_fields}
    return {"payment": payment, "delta": delta, "effective_at": effective_at}


def test_median_forecast_formula_uses_available_funds_once_and_explicit_components():
    result, inputs = calculate_forecast(
        today=date(2026, 6, 15), coverage_started_at="2026-03-01T00:00:00+00:00",
        entries=[entry("mar", 1000, -1000, "2026-03-12T12:00:00+00:00"),
                 entry("apr", 2000, -2000, "2026-04-12T12:00:00+00:00"),
                 entry("may", 3000, -3000, "2026-05-12T12:00:00+00:00"),
                 entry("jun", 500, -500, "2026-06-02T12:00:00+00:00")],
        unpaid_occurrences=[{"amount_minor": 800, "due_date": "2026-06-28",
                             "currency": "EUR", "category": "housing"}],
        paid_payment_references=[], available_funds_minor=5000,
        savings_target_minor=300, safety_buffer_minor=200,
        currency="EUR", minor_units=2)
    assert result["status"] == "available"
    assert result["projected_monthly_variable_spend_minor"] == 2000
    assert result["forecast_remaining_variable_spend_minor"] == 1500
    assert result["unpaid_commitments_minor"] == 800
    assert result["safe_to_spend_minor"] == 2200
    assert result["estimated_month_end_balance_range_minor"] == {"low": 1700, "high": 3700}
    assert result["commitment_category_breakdown"] == [{"category": "housing", "amount_minor": 800}]
    assert inputs["safe_to_spend_minor"] == 2200
    assert result["data_coverage"]["history_months"] == 3


def test_missing_or_incomplete_history_is_explicitly_unavailable():
    common = dict(today=date(2026, 6, 15), entries=[], unpaid_occurrences=[],
                  paid_payment_references=[], available_funds_minor=10000,
                  savings_target_minor=0, safety_buffer_minor=0,
                  currency="EUR", minor_units=2)
    missing, _ = calculate_forecast(coverage_started_at=None, **common)
    assert missing["status"] == "unavailable"
    assert "wallet_history_coverage_unknown" in missing["unavailable_reasons"]

    incomplete, _ = calculate_forecast(
        coverage_started_at="2025-01-01T00:00:00+00:00", history_complete=False, **common)
    assert incomplete["status"] == "unavailable"
    assert "statement_history_incomplete" in incomplete["unavailable_reasons"]
    assert incomplete["safe_to_spend_minor"] is None


def test_paid_commitment_link_excludes_wallet_outflow_exactly_once():
    result, _ = calculate_forecast(
        today=date(2026, 6, 15), coverage_started_at="2026-03-01T00:00:00+00:00",
        entries=[entry("mar", 1000, -1000, "2026-03-10T00:00:00+00:00"),
                 entry("linked-apr", 10000, -10000, "2026-04-10T00:00:00+00:00"),
                 entry("may", 1000, -1000, "2026-05-10T00:00:00+00:00")],
        unpaid_occurrences=[], paid_payment_references=["linked-apr"],
        available_funds_minor=10000, savings_target_minor=0, safety_buffer_minor=0,
        currency="EUR", minor_units=2)
    assert result["status"] == "available"
    assert result["projected_monthly_variable_spend_minor"] == 1000
    assert result["unpaid_commitments_minor"] == 0


def test_refund_offsets_only_its_linked_original_within_the_same_month():
    result, _ = calculate_forecast(
        today=date(2026, 6, 15), coverage_started_at="2026-03-01T00:00:00+00:00",
        entries=[entry("mar", 2000, -2000, "2026-03-10T00:00:00+00:00"),
                 entry("apr", 2000, -2000, "2026-04-10T00:00:00+00:00"),
                 entry("refund-source", 3000, -3000, "2026-05-10T00:00:00+00:00"),
                 entry("refund", 500, 500, "2026-05-20T00:00:00+00:00", refund_of="refund-source")],
        unpaid_occurrences=[], paid_payment_references=[], available_funds_minor=5000,
        savings_target_minor=0, safety_buffer_minor=0, currency="EUR", minor_units=2)
    assert result["projected_monthly_variable_spend_minor"] == 2000


def test_month_boundary_uses_prior_full_months_and_preserves_negative_safe_to_spend():
    result, _ = calculate_forecast(
        today=date(2026, 7, 1), coverage_started_at="2026-04-01T00:00:00+00:00",
        entries=[entry("apr", 1000, -1000, "2026-04-30T23:59:00+00:00"),
                 entry("may", 2000, -2000, "2026-05-12T12:00:00+00:00"),
                 entry("jun", 3000, -3000, "2026-06-30T23:59:00+00:00"),
                 entry("jul", 9000, -9000, "2026-07-01T00:00:00+00:00")],
        unpaid_occurrences=[{"amount_minor": 3000, "due_date": "2026-06-30",
                             "currency": "EUR", "category": "housing"}],
        paid_payment_references=[], available_funds_minor=1000,
        savings_target_minor=2000, safety_buffer_minor=1000,
        currency="EUR", minor_units=2)
    assert result["period_start"] == "2026-07-01"
    assert result["period_end"] == "2026-07-31"
    assert result["projected_monthly_variable_spend_minor"] == 2000
    assert result["current_month_variable_spend_minor"] == 9000
    assert result["forecast_remaining_variable_spend_minor"] == 0
    assert result["unpaid_commitments_minor"] == 3000
    assert result["safe_to_spend_minor"] == -5000


def test_category_breakdown_is_not_fabricated_when_wallet_has_no_categories():
    result, _ = calculate_forecast(
        today=date(2026, 6, 15), coverage_started_at="2026-03-01T00:00:00+00:00",
        entries=[entry("mar", 100, -100, "2026-03-10T00:00:00+00:00"),
                 entry("apr", 100, -100, "2026-04-10T00:00:00+00:00"),
                 entry("may", 100, -100, "2026-05-10T00:00:00+00:00")],
        unpaid_occurrences=[], paid_payment_references=[], available_funds_minor=1000,
        savings_target_minor=0, safety_buffer_minor=0, currency="EUR", minor_units=2)
    assert result["variable_category_breakdown"] == []
    assert result["data_coverage"]["variable_category_status"] == "unavailable"
