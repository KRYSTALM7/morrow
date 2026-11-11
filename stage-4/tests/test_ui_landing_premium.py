"""Acceptance checks for Morrow's scroll-driven public landing page."""
import pytest

import fixtures as fx

pytestmark = pytest.mark.stage(4)


def sel(test_id):
    return f"[data-testid='{test_id}']"


@pytest.mark.parametrize("width", [375, 768, 1440])
def test_landing_story_responsive_layout_and_ctas(reset, page, width):
    reset(fx.fixture())
    page.set_viewport_size({"width": width, "height": 900})
    page.goto("/")
    page.wait_for_selector(sel("landing-hero"))
    assert page.locator("h1").inner_text() == "Your balance isn't the whole story."
    assert page.locator(".landing-intro .eyebrow").inner_text() == "YOUR MONEY, TOMORROW."
    assert page.locator(sel("landing-signup")).get_attribute("href") == "/signup"
    assert page.locator(".landing-intro a[href='#money-clarity']").inner_text() == "See how it works"
    assert page.locator(sel("landing-final-signup")).get_attribute("href") == "/signup"
    assert page.locator(".story-scene").count() == 5
    assert page.locator(".landing-feature-section").count() == 5
    assert page.locator(".feature-paypal").inner_text().count("Sandbox") >= 2
    assert "separate from internal wallet payments" in page.locator(".feature-paypal .feature-copy").inner_text()
    assert not page.evaluate("document.documentElement.scrollWidth > window.innerWidth")
    assert page.evaluate("[...document.images].every(img => img.complete && img.naturalWidth > 0)")

    if width == 1440:
        assert page.locator(".wallet-stage").evaluate("el => getComputedStyle(el).position") == "sticky"
        widths = page.evaluate("""() => [
          document.querySelector('.landing-intro').getBoundingClientRect().width,
          document.querySelector('.wallet-stage').getBoundingClientRect().width
        ]""")
        assert abs(widths[0] - widths[1]) <= 2
    else:
        assert page.locator(".wallet-stage").evaluate("el => getComputedStyle(el).position") == "relative"
        assert page.locator(".wallet-stage").bounding_box()["height"] < 520


def test_wallet_scene_changes_follow_scroll(reset, page):
    reset(fx.fixture())
    page.set_viewport_size({"width": 1440, "height": 900})
    page.goto("/")
    stage = page.locator(".wallet-stage")
    for scene, section in enumerate(("one", "two", "three", "four", "five")):
        page.locator(f"#wallet-scene-{section}").scroll_into_view_if_needed()
        page.wait_for_function(
            "scene => document.querySelector('.wallet-stage')?.dataset.scene === scene",
            arg=str(scene), timeout=5000)
    assert stage.get_attribute("data-scene") == "4"
    assert page.locator(sel("wallet-scene-label")).inner_text() == "Financial outlook"


def test_landing_respects_reduced_motion(reset, page):
    reset(fx.fixture())
    page.emulate_media(reduced_motion="reduce")
    page.set_viewport_size({"width": 1440, "height": 900})
    page.goto("/")
    page.wait_for_selector(sel("landing-hero"))
    duration = page.locator(".wallet-lid").evaluate("el => getComputedStyle(el).transitionDuration")
    assert duration in ("1e-05s", "0.00001s", "0s")
    assert not page.evaluate("document.documentElement.scrollWidth > window.innerWidth")


def test_landing_has_static_wallet_when_intersection_observer_is_unavailable(reset, page):
    reset(fx.fixture())
    page.add_init_script("Object.defineProperty(window, 'IntersectionObserver', {configurable: true, value: undefined})")
    page.set_viewport_size({"width": 1440, "height": 900})
    page.goto("/")
    page.wait_for_selector(sel("landing-hero"))
    assert not page.evaluate("document.documentElement.classList.contains('landing-enhanced')")
    for card in page.locator(".wallet-card").all():
        assert float(card.evaluate("el => getComputedStyle(el).opacity")) == 1
    assert not page.evaluate("document.documentElement.scrollWidth > window.innerWidth")


def test_landing_has_no_console_errors_and_discloses_demo_values(reset, page):
    reset(fx.fixture())
    errors = []
    page.on("pageerror", lambda error: errors.append(str(error)))
    page.on("console", lambda message: errors.append(message.text) if message.type == "error" else None)
    page.goto("/")
    page.wait_for_selector(sel("landing-hero"))
    assert page.locator(".wallet-demo-note").inner_text() == "Illustrative demo values · not connected to an account"
    assert page.locator(".demo-unavailable").inner_text().lower().find("unavailable") >= 0
    assert errors == []
