"""Deterministic wallet-only monthly forecast and safe-to-spend calculations."""
from __future__ import annotations

import calendar
from datetime import date, datetime, timezone


MODEL_VERSION = "morrow-wallet-median-v1"
MIN_HISTORY_MONTHS = 3
MAX_HISTORY_MONTHS = 6


def _month_start(value: date) -> date:
    return value.replace(day=1)


def _shift_month(value: date, offset: int) -> date:
    ordinal = value.year * 12 + value.month - 1 + offset
    year, month0 = divmod(ordinal, 12)
    month = month0 + 1
    return date(year, month, min(value.day, calendar.monthrange(year, month)[1]))


def _month_end(value: date) -> date:
    return date(value.year, value.month, calendar.monthrange(value.year, value.month)[1])


def statement_history_start(today: date) -> date:
    """First instant needed for the six preceding complete calendar months."""
    return _shift_month(_month_start(today), -MAX_HISTORY_MONTHS)


def _iso_instant(value: date) -> datetime:
    return datetime(value.year, value.month, value.day, tzinfo=timezone.utc)


def _median(values: list[int]) -> int:
    ordered = sorted(values)
    middle = len(ordered) // 2
    if len(ordered) % 2:
        return ordered[middle]
    # Integer minor units: use deterministic half-up rounding for an even-sized median.
    return (ordered[middle - 1] + ordered[middle] + 1) // 2


def _entry_month(entry: dict) -> date:
    timestamp = datetime.fromisoformat(entry["effective_at"].replace("Z", "+00:00"))
    if timestamp.tzinfo is None:
        timestamp = timestamp.replace(tzinfo=timezone.utc)
    return timestamp.astimezone(timezone.utc).date().replace(day=1)


def calculate_forecast(*, today: date, coverage_started_at: str | None,
                       entries: list[dict], unpaid_occurrences: list[dict],
                       paid_payment_references: list[str], available_funds_minor: int,
                       savings_target_minor: int, safety_buffer_minor: int,
                       currency: str, minor_units: int,
                       history_complete: bool = True) -> tuple[dict, dict]:
    """Return a response and privacy-minimized inputs used to persist its snapshot.

    Entries follow the existing statement shape. Only negative wallet deltas count as
    variable cash outflows. Positive flows do not reduce spending; linked refunds only
    offset an original debit when both fall within the same measured month.
    """
    month_start = _month_start(today)
    month_end = _month_end(today)
    current_period_end = _shift_month(month_start, 1)
    result = {
        "status": "unavailable", "calculation_date": today.isoformat(),
        "period_start": month_start.isoformat(), "period_end": month_end.isoformat(),
        "currency": currency, "minor_units": minor_units,
        "model_version": MODEL_VERSION,
        "available_funds_minor": available_funds_minor,
        "savings_target_minor": savings_target_minor,
        "safety_buffer_minor": safety_buffer_minor,
        "forecast_remaining_variable_spend_minor": None,
        "forecast_range_minor": None,
        "unpaid_commitments_minor": None,
        "safe_to_spend_minor": None,
        "estimated_month_end_balance_range_minor": None,
        "commitment_category_breakdown": [],
        "variable_category_breakdown": [],
        "data_coverage": {"complete": False, "history_months": 0,
                           "history_start": coverage_started_at,
                           "variable_category_status": "unavailable"},
        "confidence": "unavailable",
        "assumptions": [
            "Available funds are current wallet funds after open holds; holds are deducted once here.",
            "Only this Morrow wallet ledger is observed; external accounts and cash are not included.",
            "Shared-expense receivables and expected reimbursements are excluded from available funds.",
            "Only negative wallet statement deltas count as variable outflows; income and transfers in are not projected.",
            "Only active unpaid commitment occurrences due on or before month end are reserved; paid occurrences are excluded.",
        ],
        "unavailable_reasons": [],
    }

    reasons = result["unavailable_reasons"]
    if coverage_started_at is None:
        reasons.append("wallet_history_coverage_unknown")
        return result, {"model_version": MODEL_VERSION, "history_months": [],
                        "coverage_started_at": None}
    try:
        coverage = datetime.fromisoformat(coverage_started_at.replace("Z", "+00:00"))
        if coverage.tzinfo is None:
            coverage = coverage.replace(tzinfo=timezone.utc)
    except (TypeError, ValueError):
        reasons.append("wallet_history_coverage_invalid")
        return result, {"model_version": MODEL_VERSION, "history_months": [],
                        "coverage_started_at": None}

    historical_months = []
    for back in range(1, MAX_HISTORY_MONTHS + 1):
        start = _shift_month(month_start, -back)
        if coverage > _iso_instant(start):
            break
        historical_months.append(start)
    historical_months.reverse()
    result["data_coverage"]["history_months"] = len(historical_months)
    result["data_coverage"]["history_start"] = historical_months[0].isoformat() if historical_months else None
    result["data_coverage"]["complete"] = history_complete and len(historical_months) >= MIN_HISTORY_MONTHS
    if not history_complete:
        reasons.append("statement_history_incomplete")
    if len(historical_months) < MIN_HISTORY_MONTHS:
        reasons.append("insufficient_complete_history")

    linked_payment_ids = set(paid_payment_references)
    month_spend = {value: 0 for value in historical_months}
    current_spend = 0
    month_category_spend = {value: {} for value in historical_months}
    current_category_spend = {}
    categorized_outflow_count = 0
    uncategorized_outflow_count = 0
    debit_by_id = {}
    refunds_by_id = {}
    for entry in entries:
        try:
            delta = int(entry["delta"])
            payment = entry["payment"]
            payment_id = payment["payment_id"]
            bucket = _entry_month(entry)
        except (KeyError, TypeError, ValueError):
            reasons.append("statement_entry_invalid")
            continue
        if bucket not in month_spend and bucket != month_start:
            continue
        if payment_id in linked_payment_ids:
            continue
        if delta < 0:
            amount = -delta
            debit_by_id[payment_id] = (bucket, amount)
            if bucket == month_start:
                current_spend += amount
                category = payment.get("category")
                if isinstance(category, str) and category:
                    current_category_spend[category] = current_category_spend.get(category, 0) + amount
                    categorized_outflow_count += 1
                else:
                    uncategorized_outflow_count += 1
            elif bucket in month_spend:
                month_spend[bucket] += amount
                category = payment.get("category")
                if isinstance(category, str) and category:
                    month_category_spend[bucket][category] = month_category_spend[bucket].get(category, 0) + amount
                    categorized_outflow_count += 1
                else:
                    uncategorized_outflow_count += 1
        elif delta > 0 and payment.get("refund_of"):
            refunds_by_id[payment_id] = (payment["refund_of"], bucket, delta)

    # A refund offsets only its linked original debit in the same observation month.
    for _refund_id, (original_id, refund_month, refund_amount) in refunds_by_id.items():
        original = debit_by_id.get(original_id)
        if original is None or original[0] != refund_month:
            continue
        offset = min(original[1], refund_amount)
        if refund_month == month_start:
            current_spend -= offset
        elif refund_month in month_spend:
            month_spend[refund_month] -= offset

    result["data_coverage"]["variable_category_status"] = (
        "available" if categorized_outflow_count and not uncategorized_outflow_count
        else "partial" if categorized_outflow_count else "unavailable")
    if uncategorized_outflow_count:
        result["data_coverage"]["variable_category_reason"] = "wallet_transactions_have_no_category"

    commitment_categories = {}
    commitments_total = 0
    for occurrence in unpaid_occurrences:
        if occurrence.get("currency") != currency:
            reasons.append("commitment_currency_mismatch")
            continue
        try:
            amount = int(occurrence["amount_minor"])
            due = date.fromisoformat(occurrence["due_date"])
        except (KeyError, TypeError, ValueError):
            reasons.append("commitment_occurrence_invalid")
            continue
        if amount < 0 or due > month_end:
            continue
        commitments_total += amount
        category = occurrence.get("category") or "uncategorized"
        commitment_categories[category] = commitment_categories.get(category, 0) + amount
    result["unpaid_commitments_minor"] = commitments_total
    result["commitment_category_breakdown"] = [
        {"category": category, "amount_minor": amount}
        for category, amount in sorted(commitment_categories.items())]

    input_summary = {"model_version": MODEL_VERSION,
                     "coverage_started_at": coverage_started_at,
                     "history_months": [value.isoformat() for value in historical_months],
                     "monthly_variable_outflows_minor": [month_spend[value] for value in historical_months],
                     "current_month_variable_outflows_minor": current_spend,
                     "unpaid_commitments_minor": commitments_total,
                     "savings_target_minor": savings_target_minor,
                     "safety_buffer_minor": safety_buffer_minor,
                     "available_funds_minor": available_funds_minor}
    if reasons:
        result["unavailable_reasons"] = sorted(set(reasons))
        return result, input_summary

    historical_totals = [month_spend[value] for value in historical_months]
    projected_monthly_outflow = _median(historical_totals)
    remaining = max(0, projected_monthly_outflow - current_spend)
    low_remaining = max(0, min(historical_totals) - current_spend)
    high_remaining = max(0, max(historical_totals) - current_spend)
    safe = (available_funds_minor - commitments_total - remaining
            - savings_target_minor - safety_buffer_minor)
    month_end_low = available_funds_minor - commitments_total - high_remaining
    month_end_high = available_funds_minor - commitments_total - low_remaining
    result.update({
        "status": "available",
        "forecast_remaining_variable_spend_minor": remaining,
        "forecast_range_minor": {"low": low_remaining, "high": high_remaining},
        "safe_to_spend_minor": safe,
        "estimated_month_end_balance_range_minor": {"low": month_end_low, "high": month_end_high},
        "projected_monthly_variable_spend_minor": projected_monthly_outflow,
        "current_month_variable_spend_minor": current_spend,
        "confidence": "high" if len(historical_months) >= 6 else "medium" if len(historical_months) >= 4 else "low",
        "data_coverage": {**result["data_coverage"], "complete": True,
                           "history_months": len(historical_months),
                           "history_start": historical_months[0].isoformat()},
    })
    category_available = result["data_coverage"]["variable_category_status"] == "available"
    if category_available:
        categories = set().union(*(set(month_category_spend[value]) for value in historical_months))
        result["variable_category_breakdown"] = [
            {"category": category,
             "monthly_median_minor": _median([month_category_spend[value].get(category, 0)
                                               for value in historical_months])}
            for category in sorted(categories)]
    result["assumptions"].extend([
        f"Remaining variable spend uses the median of {len(historical_months)} complete prior calendar months, less this month’s observed variable outflows.",
        "Future income is not projected.",
        "A commitment payment is excluded from wallet outflow history only when its payment_reference exactly matches a wallet payment_id.",
    ])
    input_summary["projected_monthly_variable_spend_minor"] = projected_monthly_outflow
    input_summary["forecast_remaining_variable_spend_minor"] = remaining
    input_summary["safe_to_spend_minor"] = safe
    return result, input_summary
