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
    expect(page.locator(sel("wallet-available"))).to_have_text("100.00 EUR")
    expect(page.locator(sel("safe-to-spend-explanation"))).to_contain_text("At least three complete months")
    assert page.text_content(sel("safe-to-spend-status")) == "Unavailable"
    assert "No safe-to-spend amount shown" in page.text_content(sel("safe-to-spend-explanation"))
    expect(page.locator(sel("commitments-overview-content"))).to_have_text("0 active commitments")
    assert "At least three complete months" in page.text_content(sel("forecast-overview-content"))
    assert "Reimbursements stay separate from wallet funds" in page.text_content(sel("shared-expenses-overview"))
    assert page.text_content(sel("dashboard-month-in-value")) == "0.00 EUR"
    assert page.text_content(sel("dashboard-month-out-value")) == "0.00 EUR"
    assert page.locator("#pay-form, #request-form, #authorize-form").count() == 0
    assert page.locator(".quick-action-links a").count() == 3
    assert page.locator(".dashboard-overview").evaluate("el => [...el.children].map(x => x.classList[0])")[:4] == [
        "panel", "dashboard-stat-grid", "dashboard-quick-actions", "dashboard-summary-grid"
    ]


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
    expect(page.locator(sel("commitments-overview-content"))).to_have_text("0 active commitments")


def test_dashboard_can_save_savings_target_and_safety_buffer(reset, page):
    reset(fx.fixture())
    login(page)
    page.goto("/assistant#financial-outlook")
    page.wait_for_selector(sel("forecast-settings-form"))
    page.fill(sel("forecast-savings-target"), "15.00")
    page.fill(sel("forecast-safety-buffer"), "5.00")
    page.click(sel("forecast-settings-save"))
    expect(page.locator(sel("forecast-settings-success"))).to_have_text("Forecast targets saved.")
    assert page.input_value(sel("forecast-savings-target")) == "15.00"
    assert page.input_value(sel("forecast-safety-buffer")) == "5.00"


def test_overview_quick_actions_open_the_existing_payment_workflow(reset, page):
    reset(fx.fixture())
    login(page)
    page.goto("/")
    page.wait_for_selector(".quick-action-links a")
    assert page.locator(".quick-action-links a").evaluate_all("els => els.map(el => el.getAttribute('href'))") == [
        "/assistant#pay-form", "/assistant#request-form", "/assistant#authorize-form"
    ]
    assert page.locator(".dashboard-preview a[href='/commitments']").count() == 1
    assert page.locator(".dashboard-preview a[href='/activity']").count() == 1
    assert page.locator(".dashboard-preview a[href='/assistant#financial-outlook']").count() == 1


def test_money_action_forms_live_on_assistant_and_submit_request_and_reserve(reset, page, api):
    reset(fx.fixture())
    login(page)
    page.goto("/assistant#request-form")
    for form in ("pay-form", "request-form", "authorize-form"):
        assert page.locator(f"#{form}").is_visible()

    page.fill(sel("request-handle"), "bob")
    page.fill(sel("request-amount"), "12.50")
    page.click(sel("request-submit"))
    expect(page.locator(sel("request-form-success"))).to_be_visible()
    ada = api().authenticate(fx.ADA["email"], fx.ADA["password"])
    requests = ada.get("/requests?status=pending").json()["requests"]
    assert any(request["amount"] == 1250 for request in requests)

    page.fill(sel("authorize-handle"), "bob")
    page.fill(sel("authorize-amount"), "8.00")
    page.click(sel("authorize-submit"))
    expect(page.locator(sel("authorize-form-success"))).to_be_visible()
    assert len(ada.get("/authorizations").json()["authorizations"]) == 1


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


def test_wallet_expands_to_verified_financial_picture_and_collapses_by_button(reset, page, api):
    reset(fx.fixture())
    ada = api().authenticate(fx.ADA["email"], fx.ADA["password"])
    ada.post("/authorizations", json={"to_handle": "bob", "amount": 500}, idempotency_key=new_key())
    ada.post("/commitments", json={"title": "Monthly rent", "category": "housing",
                                    "amount_minor": 120000, "currency": "EUR", "frequency": "monthly",
                                    "next_due_date": "2026-12-01"}, idempotency_key=new_key())
    ada.post("/shared-expenses", json={"description": "Dinner", "category": "food",
                                        "gross_amount_minor": 1200, "personal_share_minor": 400,
                                        "currency": "EUR", "participants": [{"handle": "bob", "share_minor": 800}]},
             idempotency_key=new_key())
    ada.post("/payments", json={"to_handle": "bob", "amount": 250}, idempotency_key=new_key())
    login(page)
    page.goto("/")

    wallet = page.locator(sel("wallet-card"))
    toggle = page.get_by_role("button", name="View full financial picture")
    expect(toggle).to_have_attribute("aria-expanded", "false")
    assert page.locator("#wallet-expanded-content").get_attribute("aria-hidden") == "true"
    toggle.click()
    expect(wallet).to_have_class("panel wallet-hero wallet-expanded")
    expect(page.locator(sel("wallet-position-content"))).to_contain_text("Available wallet funds")
    expect(page.locator(sel("wallet-position-content"))).to_contain_text("Money spent this month")
    expect(page.locator(sel("wallet-holds-content"))).to_contain_text("1 open hold")
    expect(page.locator(sel("wallet-commitments-content"))).to_contain_text("Monthly rent")
    expect(page.locator(sel("wallet-shared-expenses-content"))).to_contain_text("8.00 EUR")
    expect(page.locator(sel("wallet-transactions-content"))).to_contain_text("Paid bob")
    expect(page.locator(sel("wallet-outlook-content"))).to_contain_text("Safe-to-Spend unavailable")
    collapse = page.get_by_role("button", name="Collapse financial picture")
    expect(collapse).to_have_attribute("aria-expanded", "true")
    collapse.click()
    expect(wallet).to_have_class("panel wallet-hero")
    expect(page.locator(sel("wallet-toggle"))).to_have_attribute("aria-expanded", "false")


@pytest.mark.parametrize("width", [375, 390, 768, 1024, 1440])
def test_expanded_wallet_fits_required_viewports(reset, page, width):
    reset(fx.fixture())
    page.set_viewport_size({"width": width, "height": 900})
    login(page)
    page.goto("/")
    page.get_by_role("button", name="View full financial picture").click()
    expect(page.locator(sel("wallet-expanded-content"))).to_be_visible()
    page.wait_for_timeout(100)
    sizes = page.evaluate("({width: document.documentElement.clientWidth, scroll: document.documentElement.scrollWidth, wallet: document.querySelector('[data-testid=wallet-card]').getBoundingClientRect().width})")
    assert sizes["scroll"] <= sizes["width"]
    assert sizes["wallet"] > 0


def test_wallet_card_click_and_keyboard_expand_and_wallet_data_error_is_clear(reset, page):
    reset(fx.fixture())
    login(page)
    page.route("**/safe-to-spend", lambda route: route.abort())
    page.goto("/")
    wallet = page.locator(sel("wallet-card"))
    wallet.locator(".currency-caption").click()
    assert "wallet-expanded" in wallet.get_attribute("class").split()
    wallet.focus()
    page.keyboard.press("Enter")
    assert "wallet-expanded" not in wallet.get_attribute("class").split()

    page.route("**/shared-expenses**", lambda route: route.abort())
    page.get_by_role("button", name="View full financial picture").click()
    expect(page.locator(sel("wallet-shared-expenses-content") + " .error-message")).to_contain_text("Could not load shared expenses")
    expect(page.locator(sel("wallet-outlook-content") + " .error-message")).to_contain_text("reach Morrow")


@pytest.mark.parametrize("width", [1024, 1440])
def test_desktop_sidebar_collapses_to_icon_rail_and_restores(reset, page, width):
    reset(fx.fixture())
    page.set_viewport_size({"width": width, "height": 900})
    login(page)
    toggle = page.locator("#menu-toggle")
    expect(toggle).to_have_attribute("aria-expanded", "true")
    toggle.focus()
    page.keyboard.press("Enter")
    expect(toggle).to_have_attribute("aria-expanded", "false")
    expect(page.locator("body")).to_have_class("sidebar-collapsed")
    expect(page.locator(".main-nav a[aria-current='page']")).to_have_attribute("aria-label", "Overview")
    assert page.locator(".main-nav a .nav-label").first.is_hidden()
    page.keyboard.press("Enter")
    expect(toggle).to_have_attribute("aria-expanded", "true")
    assert page.locator(".main-nav a .nav-label").first.is_visible()


@pytest.mark.parametrize("width", [320, 375, 390, 760, 1024, 1280, 1440])
def test_sidebar_and_mobile_navigation_have_active_state_without_page_overflow(reset, page, width):
    reset(fx.fixture())
    page.set_viewport_size({"width": width, "height": 900})
    login(page)
    page.goto("/requests")
    active = page.locator(".main-nav a[href='/requests']")
    assert page.locator(".bottom-nav").count() == 0
    if width <= 760:
        assert page.locator(".app-sidebar").evaluate("el => getComputedStyle(el).position") == "fixed"
        assert page.locator("#menu-toggle").get_attribute("aria-expanded") == "false"
        assert not page.locator(".app-sidebar").is_visible()
        page.locator("#menu-toggle").click()
        assert page.locator("#menu-toggle").get_attribute("aria-expanded") == "true"
        assert active.get_attribute("aria-current") == "page"
        assert active.text_content() == "Requests"
        page.keyboard.press("Escape")
        assert page.locator("#menu-toggle").get_attribute("aria-expanded") == "false"
    else:
        assert page.locator(".app-sidebar").is_visible()
        assert active.get_attribute("aria-current") == "page"
        assert active.text_content() == "Requests"
    assert not page.evaluate("document.documentElement.scrollWidth > window.innerWidth")
