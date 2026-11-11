"""Dashboard totals and navigation use only current authenticated wallet APIs."""
import pytest
from playwright.sync_api import expect

import fixtures as fx
from harness.http import new_key

pytestmark = pytest.mark.stage(4)


def sel(test_id):
    return f"[data-testid='{test_id}']"


def login(page, email=fx.ADA["email"]):
    page.goto("/login")
    page.fill(sel("login-email"), email)
    page.fill(sel("login-password"), fx.ADA["password"])
    page.click(sel("login-submit"))
    page.wait_for_selector(sel("current-user"))


def test_dashboard_uses_wallet_balance_and_discloses_incomplete_forecast_inputs(reset, page):
    reset(fx.fixture())
    login(page)
    page.goto("/")
    page.wait_for_selector(sel("dashboard-month-in-value"))
    assert page.text_content(sel("wallet-available")) == "100.00 EUR"
    expect(page.locator(sel("safe-to-spend-explanation"))).to_contain_text("At least three complete months")
    assert page.text_content(sel("safe-to-spend-status")) == "Unavailable"
    assert "No safe-to-spend amount shown" in page.text_content(sel("safe-to-spend-explanation"))
    assert "Unpaid through" in page.text_content(sel("commitments-overview-content"))
    assert "At least three complete months" in page.text_content(sel("forecast-overview-content"))
    assert "Shared-expense shares and reimbursements aren’t consolidated" in page.text_content(sel("shared-expenses-overview"))
    assert page.text_content(sel("dashboard-month-in-value")) == "0.00 EUR"
    assert page.text_content(sel("dashboard-month-out-value")) == "0.00 EUR"


def test_month_to_date_flow_totals_come_from_user_statement(reset, page, api):
    reset(fx.fixture())
    ada = api().authenticate(fx.ADA["email"], fx.ADA["password"])
    bob = api().authenticate(fx.BOB["email"], fx.BOB["password"])
    ada.post("/payments", json={"to_handle": "bob", "amount": 1200}, idempotency_key=new_key())
    bob.post("/payments", json={"to_handle": "ada", "amount": 350}, idempotency_key=new_key())

    login(page)
    page.goto("/")
    expect(page.locator(sel("dashboard-month-in-value"))).to_have_text("3.50 EUR")
    expect(page.locator(sel("dashboard-month-out-value"))).to_have_text("12.00 EUR")


def test_open_request_summary_is_pulled_from_existing_p2p_request_api(reset, page, api):
    reset(fx.fixture())
    ada = api().authenticate(fx.ADA["email"], fx.ADA["password"])
    bob = api().authenticate(fx.BOB["email"], fx.BOB["password"])
    bob.post("/requests", json={"payer_handle": "ada", "amount": 500}, idempotency_key=new_key())
    ada.post("/requests", json={"payer_handle": "bob", "amount": 250}, idempotency_key=new_key())

    login(page)
    page.goto("/")
    expect(page.locator(sel("shared-request-summary"))).to_contain_text("5.00 EUR")
    summary = page.text_content(sel("shared-request-summary"))
    assert "Requested from you · bob" in summary
    assert "5.00 EUR" in summary
    assert "You requested · bob" in summary
    assert "2.50 EUR" in summary


def test_dashboard_explains_unavailable_forecast_without_complete_history(reset, page):
    reset(fx.fixture())
    login(page)
    page.goto("/")
    expect(page.locator(sel("safe-to-spend-explanation"))).to_contain_text("At least three complete months")
    assert "At least three complete months" in page.text_content(sel("safe-to-spend-explanation"))
    assert "History coverage incomplete" in page.text_content(sel("forecast-overview-content"))
    assert "Unpaid through" in page.text_content(sel("commitments-overview-content"))


def test_dashboard_can_save_savings_target_and_safety_buffer(reset, page):
    reset(fx.fixture())
    login(page)
    page.goto("/")
    page.wait_for_selector(sel("forecast-settings-form"))
    expect(page.locator(sel("safe-to-spend-explanation"))).to_contain_text("At least three complete months")
    page.fill(sel("forecast-savings-target"), "15.00")
    page.fill(sel("forecast-safety-buffer"), "5.00")
    page.click(sel("forecast-settings-save"))
    expect(page.locator(sel("forecast-settings-success"))).to_have_text("Forecast targets saved.")
    assert page.input_value(sel("forecast-savings-target")) == "15.00"
    assert page.input_value(sel("forecast-safety-buffer")) == "5.00"


def test_financial_companion_shows_grounded_unavailable_state_and_suggestions(reset, page):
    reset(fx.fixture())
    login(page)
    page.goto("/assistant")
    assert page.locator(sel("companion-message")).is_visible()
    page.fill(sel("companion-message"), "Why is my Safe-to-Spend amount this month?")
    page.locator(".companion-panel form button[type='submit']").click()
    response = page.locator(".companion-response")
    expect(response).to_contain_text("Safe-to-Spend is unavailable")
    expect(response).to_contain_text("three complete months")
    expect(response).to_contain_text("Verified snapshot")
    expect(response).to_contain_text("Unavailable")


def test_dashboard_recent_transactions_exclude_public_activity_for_other_wallets(reset, page, api):
    reset(fx.fixture())
    cy = api().authenticate(fx.CY["email"], fx.CY["password"])
    payment_id = cy.post("/payments", json={"to_handle": "bob", "amount": 125, "visibility": "public"},
                         idempotency_key=new_key()).json()["payment_id"]
    login(page)
    page.goto("/")
    page.wait_for_selector(sel("empty-activity"))
    assert page.query_selector(sel(f"activity-item-{payment_id}")) is None


@pytest.mark.parametrize("width", [320, 375, 760, 1280])
def test_sidebar_and_mobile_navigation_have_active_state_without_page_overflow(reset, page, width):
    reset(fx.fixture())
    page.set_viewport_size({"width": width, "height": 900})
    login(page)
    page.goto("/requests")
    active = page.locator(".main-nav a[aria-current='page']")
    assert active.inner_text() == "Requests"
    if width <= 760:
        assert page.locator(".app-sidebar").evaluate("el => getComputedStyle(el).position") == "fixed"
    else:
        assert page.locator(".app-sidebar").is_visible()
    assert not page.evaluate("document.documentElement.scrollWidth > window.innerWidth")
