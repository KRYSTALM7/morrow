"""The UI works under its Content-Security-Policy and stays usable on small screens."""
import pytest
from playwright.sync_api import expect

import fixtures as fx
from harness.http import assert_status, new_key

pytestmark = pytest.mark.stage(2)


def sel(test_id):
    return f"[data-testid='{test_id}']"


def login(page, email):
    page.goto("/login")
    page.fill(sel("login-email"), email)
    page.fill(sel("login-password"), fx.ADA["password"])
    page.click(sel("login-submit"))
    page.wait_for_selector(sel("current-user"))


def test_ui_routes_raise_no_csp_violations(reset, page):
    reset(fx.fixture())
    violations = []
    page.on("console", lambda message: violations.append(message.text)
            if "Content Security Policy" in message.text else None)
    login(page, fx.ADA["email"])
    for route in ("/", "/requests", "/split", "/authorizations"):
        page.goto(route)
        page.wait_for_load_state("networkidle")
    assert violations == []


@pytest.mark.parametrize("width", [375, 760])
def test_mobile_drawer_receives_focus_and_fits_small_screens(reset, page, width):
    reset(fx.fixture())
    page.set_viewport_size({"width": width, "height": 800})
    login(page, fx.ADA["email"])
    page.locator("#menu-toggle").click()
    expect(page.locator("#drawer-close")).to_be_focused()
    assert page.evaluate("document.activeElement.id") == "drawer-close"
    page.wait_for_timeout(300)
    bounds = page.locator(".app-sidebar").bounding_box()
    assert bounds["x"] == 0 and 0 < bounds["width"] <= width
    assert page.locator(".main-nav a").count() == 5


def test_mobile_drawer_closes_with_escape_overlay_and_navigation(reset, page):
    reset(fx.fixture())
    page.set_viewport_size({"width": 390, "height": 844})
    login(page, fx.ADA["email"])
    toggle = page.locator("#menu-toggle")
    toggle.click()
    expect(page.locator("body")).to_have_class("nav-open")
    assert page.evaluate("getComputedStyle(document.body).overflow") == "hidden"
    page.keyboard.press("Escape")
    expect(toggle).to_have_attribute("aria-expanded", "false")
    expect(toggle).to_be_focused()
    assert page.evaluate("getComputedStyle(document.body).overflow") != "hidden"

    toggle.click()
    page.locator("#drawer-overlay").click(position={"x": 350, "y": 300})
    expect(toggle).to_have_attribute("aria-expanded", "false")
    toggle.click()
    page.locator(".main-nav a[href='/requests']").click()
    expect(toggle).to_have_attribute("aria-expanded", "false")
    assert page.url.endswith("/requests")


def test_hold_expiry_is_labelled_and_keeps_its_exact_value(reset, page, api):
    reset(fx.fixture())
    ada = api().authenticate(fx.ADA["email"], fx.ADA["password"])
    authorization = assert_status(ada.post(
        "/authorizations", json={"to_handle": "bob", "amount": 500},
        idempotency_key=new_key()), 201).json()
    login(page, fx.ADA["email"])
    page.goto("/authorizations")
    expiry = page.locator(sel(f"authorization-expires-{authorization['authorization_id']}"))
    assert expiry.inner_text() == authorization["expires_at"]
    assert expiry.evaluate("el => el.parentElement.textContent").startswith("Expires ")
