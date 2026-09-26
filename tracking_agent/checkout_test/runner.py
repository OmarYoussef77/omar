"""Drive a real (headless) Chromium through the store and record every
tracking request per step.

Browse mode (default) stops at checkout: no order is created, so it's safe
to run on a schedule. Purchase mode pays with Shopify's Bogus Gateway, which
only works while the store's payments are in test mode; with a real gateway
the test card is simply declined.

Shopify's checkout markup changes over time and themes differ, so every
selector can be overridden in config (checkout_test.selectors).
"""

from __future__ import annotations

import os
import time
from typing import Any, Callable

from .capture import Hit, classify
from .report import evaluate

DEFAULT_SELECTORS = {
    "password_input": "input[type='password']",
    "add_to_cart": "form[action*='/cart/add'] [type='submit'], form[action*='/cart/add'] [name='add']",
    "email": "input[name='email'], #email",
    "first_name": "input[name='firstName']",
    "last_name": "input[name='lastName']",
    "address1": "input[name='address1']",
    "city": "input[name='city']",
    "zone": "select[name='zone']",
    "postal_code": "input[name='postalCode']",
    "phone": "input[name='phone']",
    "card_number_frame": "iframe[name^='card-fields-number']",
    "card_number": "input[name='number'], #number",
    "card_expiry_frame": "iframe[name^='card-fields-expiry']",
    "card_expiry": "input[name='expiry'], #expiry",
    "card_cvv_frame": "iframe[name^='card-fields-verification_value']",
    "card_cvv": "input[name='verification_value'], #verification_value",
    "card_name_frame": "iframe[name^='card-fields-name']",
    "card_name": "input[name='name'], #name",
    "pay_button": "#checkout-pay-button, button[type='submit']:has-text('Pay now')",
}

# Shopify Bogus Gateway: card "1" = approved.
BOGUS_CARD = {"number": "1", "expiry": "12 / 30", "cvv": "123", "name": "Bogus Gateway"}

CHROMIUM_FALLBACKS = ("/opt/pw-browsers/chromium",)


def _launch(playwright, headless: bool):
    path = os.environ.get("TRACKING_AGENT_CHROMIUM")
    if path:
        return playwright.chromium.launch(headless=headless, executable_path=path)
    try:
        return playwright.chromium.launch(headless=headless)
    except Exception:
        for candidate in CHROMIUM_FALLBACKS:
            if os.path.exists(candidate):
                return playwright.chromium.launch(headless=headless, executable_path=candidate)
        raise


def store_base_url(config: dict[str, Any]) -> str:
    url = config["checkout_test"].get("store_url") or config["store"].get("domain", "")
    if not url:
        raise ValueError("Set checkout_test.store_url or store.domain")
    if not url.startswith(("http://", "https://")):
        url = "https://" + url
    return url.rstrip("/")


def run_checkout_test(
    config: dict[str, Any],
    *,
    purchase: bool = False,
    headless: bool | None = None,
    settle_ms: int = 3000,
    configure_context: Callable[[Any], None] | None = None,
) -> dict[str, Any]:
    """Run the journey and return the evaluated report (see report.evaluate)."""
    try:
        from playwright.sync_api import sync_playwright
    except ImportError as exc:  # pragma: no cover
        raise RuntimeError("Test checkout needs Playwright: pip install 'tracking-agent[browser]'") from exc

    settings = config["checkout_test"]
    selectors = {**DEFAULT_SELECTORS, **(settings.get("selectors") or {})}
    base = store_base_url(config)
    capi = config.get("capi") or {}
    collect_url = capi["server_url"].rstrip("/") + "/collect" if capi.get("enabled") and capi.get("server_url") else ""

    hits: list[Hit] = []
    reached: list[str] = []
    errors: list[str] = []
    state = {"step": "page_view"}

    def on_request(request) -> None:
        try:
            body = request.post_data
        except Exception:  # binary body
            body = None
        for hit in classify(request.url, request.method, body, collect_url):
            hit.step = state["step"]
            hits.append(hit)

    def enter(step: str) -> None:
        state["step"] = step

    def settle(page) -> None:
        try:
            page.wait_for_load_state("networkidle", timeout=15000)
        except Exception:
            pass
        page.wait_for_timeout(settle_ms)

    started = time.time()
    with sync_playwright() as playwright:
        browser = _launch(playwright, settings.get("headless", True) if headless is None else headless)
        context = browser.new_context(locale="en-US", viewport={"width": 1366, "height": 900})
        if configure_context:
            configure_context(context)
        context.on("request", on_request)
        page = context.new_page()
        page.set_default_timeout(30000)
        try:
            enter("page_view")
            page.goto(base + "/")
            if "/password" in page.url:
                password = os.environ.get(settings.get("store_password_env") or "STORE_PASSWORD", "")
                if not password:
                    raise RuntimeError("Store is password protected: set the STORE_PASSWORD environment variable")
                page.fill(selectors["password_input"], password)
                page.keyboard.press("Enter")
                page.wait_for_load_state()
                page.goto(base + "/")
            settle(page)
            reached.append("page_view")

            enter("view_item")
            product_url = settings.get("product_url") or _first_product_url(page, base)
            page.goto(product_url if product_url.startswith("http") else base + product_url)
            settle(page)
            reached.append("view_item")

            enter("add_to_cart")
            with page.expect_response(lambda r: "/cart/add" in r.url, timeout=20000):
                page.locator(selectors["add_to_cart"]).first.click()
            settle(page)
            reached.append("add_to_cart")

            enter("begin_checkout")
            page.goto(base + "/checkout")
            page.locator(selectors["email"]).first.wait_for(timeout=30000)
            settle(page)
            reached.append("begin_checkout")

            if purchase:
                _fill_checkout(page, selectors, settings)
                _fill_card(page, selectors)
                enter("purchase")
                page.locator(selectors["pay_button"]).first.click()
                page.wait_for_url(lambda url: "thank" in url or "/orders/" in url, timeout=90000)
                settle(page)
                page.wait_for_timeout(settle_ms)  # thank-you pixels load late
                reached.append("purchase")
        except Exception as exc:
            errors.append(f"{state['step']}: {type(exc).__name__}: {str(exc).splitlines()[0][:300]}")
        finally:
            final_url = page.url
            browser.close()

    report = evaluate(hits, config, purchase=purchase, reached=reached)
    report.update(
        {
            "mode": "purchase" if purchase else "browse",
            "store": base,
            "final_url": final_url,
            "errors": errors,
            "seconds": round(time.time() - started, 1),
            "hits": [h.to_dict() for h in hits],
        }
    )
    if errors:
        report["status"] = "fail"
    return report


def _first_product_url(page, base: str) -> str:
    response = page.request.get(base + "/products.json?limit=1")
    products = response.json().get("products") if response.ok else None
    if not products:
        raise RuntimeError("No products found at /products.json; set checkout_test.product_url")
    return f"{base}/products/{products[0]['handle']}"


def _fill_checkout(page, selectors: dict[str, str], settings: dict[str, Any]) -> None:
    address = settings.get("address") or {}
    page.locator(selectors["email"]).first.fill(settings.get("email") or "tracking-test@example.com")
    for key in ("first_name", "last_name", "address1", "city", "postal_code", "phone"):
        value = address.get(key)
        field = page.locator(selectors[key]).first
        if value and field.count():
            field.fill(str(value))
    zone = page.locator(selectors["zone"]).first
    if address.get("zone") and zone.count():
        zone.select_option(address["zone"])


def _fill_card(page, selectors: dict[str, str]) -> None:
    for frame_key, input_key, value in (
        ("card_number_frame", "card_number", BOGUS_CARD["number"]),
        ("card_expiry_frame", "card_expiry", BOGUS_CARD["expiry"]),
        ("card_cvv_frame", "card_cvv", BOGUS_CARD["cvv"]),
        ("card_name_frame", "card_name", BOGUS_CARD["name"]),
    ):
        frame = page.frame_locator(selectors[frame_key]).first
        field = frame.locator(selectors[input_key]).first
        field.wait_for(timeout=30000)
        field.fill(value)
