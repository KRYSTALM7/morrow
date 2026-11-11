"""Signup and login validation, feedback, and responsive layout."""
import pytest

import fixtures as fx

pytestmark = pytest.mark.stage(4)


def sel(test_id):
    return f"[data-testid='{test_id}']"


def test_signup_validates_fields_and_password_visibility(reset, page):
    reset(fx.fixture())
    page.goto("/signup")
    page.click(sel("signup-submit"))
    assert page.locator(sel("signup-email-error")).inner_text() == "Enter your email address."
    assert page.locator("#signup-email").get_attribute("aria-invalid") == "true"
    assert page.locator("#signup-email").evaluate("el => el === document.activeElement")

    page.fill("#signup-email", "not-an-email")
    page.fill("#signup-display-name", "   ")
    page.fill("#signup-password", "short")
    page.click(sel("signup-submit"))
    assert page.locator(sel("signup-email-error")).inner_text() == "Enter a valid email address."
    assert page.locator(sel("signup-display-name-error")).inner_text() == "Enter the name you’d like people to see."
    assert page.locator(sel("signup-password-error")).inner_text() == "Use at least 8 characters for your password."

    page.fill("#signup-password", "correct horse")
    visibility = page.locator("[data-testid='signup-password-visibility']")
    assert page.locator("#signup-password").get_attribute("type") == "password"
    visibility.click()
    assert page.locator("#signup-password").get_attribute("type") == "text"
    assert visibility.get_attribute("aria-pressed") == "true"
    visibility.click()
    assert page.locator("#signup-password").get_attribute("type") == "password"


def test_signup_success_and_duplicate_email_feedback(reset, page):
    reset(fx.fixture())
    page.goto("/signup")
    page.fill("#signup-email", "fresh.person@example.com")
    page.fill("#signup-display-name", "Fresh Person")
    page.fill("#signup-password", "a long passphrase")
    page.click(sel("signup-submit"))
    assert "account is ready" in page.locator(sel("auth-success")).inner_text()
    page.wait_for_selector(sel("wallet-card"))
    assert page.locator(sel("current-user")).inner_text() == "Fresh Person"

    page.evaluate("localStorage.removeItem('pocketful.token')")
    page.goto("/signup")
    page.fill("#signup-email", "fresh.person@example.com")
    page.fill("#signup-display-name", "Another Name")
    page.fill("#signup-password", "a long passphrase")
    page.click(sel("signup-submit"))
    assert "already registered" in page.locator(sel("auth-error")).inner_text()
    assert page.locator(".auth-switch a").get_attribute("href") == "/login"
    assert not page.locator(sel("signup-submit")).is_disabled()


def test_signup_failed_request_has_recoverable_error(reset, page):
    reset(fx.fixture())
    page.goto("/signup")
    page.fill("#signup-email", "offline.person@example.com")
    page.fill("#signup-display-name", "Offline Person")
    page.fill("#signup-password", "a long passphrase")
    page.route("**/auth/signup", lambda route: route.abort())
    page.click(sel("signup-submit"))
    assert "couldn’t reach Morrow" in page.locator(sel("auth-error")).inner_text()
    assert not page.locator(sel("signup-submit")).is_disabled()


def test_signup_prevents_duplicate_submissions(reset, page):
    reset(fx.fixture())
    page.add_init_script("""
      window.signupRequestCount = 0;
      const originalFetch = window.fetch.bind(window);
      window.fetch = (input, options) => {
        if (String(input) === '/auth/signup') {
          window.signupRequestCount += 1;
          return new Promise(resolve => {
            window.finishSignup = () => resolve(new Response(JSON.stringify({token: 'test-token'}), {
              status: 201, headers: {'Content-Type': 'application/json'}
            }));
          });
        }
        return originalFetch(input, options);
      };
    """)
    page.goto("/signup")
    page.fill("#signup-email", "one.submit@example.com")
    page.fill("#signup-display-name", "One Submit")
    page.fill("#signup-password", "a long passphrase")
    page.click(sel("signup-submit"))
    assert page.locator(sel("signup-submit")).is_disabled()
    page.evaluate("document.querySelector('form').requestSubmit()")
    assert page.evaluate("window.signupRequestCount") == 1
    page.evaluate("window.finishSignup()")


def test_login_success_and_mobile_auth_card_fit(reset, page):
    reset(fx.fixture())
    for width in (375, 320):
        page.set_viewport_size({"width": width, "height": 800})
        page.goto("/signup")
        assert not page.evaluate("document.documentElement.scrollWidth > window.innerWidth")
        signup_card = page.locator(".auth-card").bounding_box()
        assert signup_card["x"] >= 0 and signup_card["x"] + signup_card["width"] <= width

        page.goto("/login")
        page.fill("#login-email", fx.ADA["email"])
        page.fill("#login-password", fx.ADA["password"])
        assert not page.evaluate("document.documentElement.scrollWidth > window.innerWidth")
        card = page.locator(".auth-card").bounding_box()
        assert card["x"] >= 0 and card["x"] + card["width"] <= width
        if width == 375:
            page.locator("[data-testid='login-password-visibility']").click()
            assert page.locator("#login-password").get_attribute("type") == "text"
    page.click(sel("login-submit"))
    page.wait_for_selector(sel("wallet-card"))
    assert page.locator(sel("current-user")).inner_text() == fx.ADA["display_name"]
