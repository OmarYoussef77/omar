"""Compare captured hits with what the config says should fire at each step."""

from __future__ import annotations

from collections import Counter
from typing import Any

from ..config import enabled_platforms
from ..gtm_builder import META_EVENTS, SNAP_EVENTS, TIKTOK_EVENTS
from .capture import Hit

STEPS = ("page_view", "view_item", "add_to_cart", "begin_checkout", "add_payment_info", "purchase")
PURCHASE_STEPS = ("add_payment_info", "purchase")


def _ga4(event: str) -> str:
    return event


EXPECTED_EVENT = {
    "meta": META_EVENTS.get,
    "tiktok": lambda e: "Pageview" if e == "page_view" else TIKTOK_EVENTS.get(e),
    "snapchat": SNAP_EVENTS.get,
    "ga4": _ga4,
}

PIXEL_FIELD = {
    "ga4": "measurement_id",
    "google_ads": "conversion_id",
    "meta": "pixel_id",
    "tiktok": "pixel_id",
    "snapchat": "pixel_id",
    "linkedin": "partner_id",
}


def expected_events(config: dict[str, Any], step: str) -> dict[str, str]:
    """{platform: event name} that must be seen during this step."""
    out: dict[str, str] = {}
    for platform in enabled_platforms(config):
        settings = config["platforms"][platform]
        if platform in EXPECTED_EVENT:
            name = EXPECTED_EVENT[platform](step)
            if name:
                out[platform] = name
        elif platform == "linkedin":
            if step == "page_view":
                out[platform] = "pageview"
            elif step in (settings.get("conversions") or {}):
                out[platform] = f"conversion:{settings['conversions'][step]}"
        elif platform == "google_ads":
            label = (settings.get("conversions") or {}).get(step)
            if label:
                out[platform] = f"conversion:{label}"
    return out


def evaluate(hits: list[Hit], config: dict[str, Any], *, purchase: bool, reached: list[str]) -> dict[str, Any]:
    steps = [s for s in STEPS if purchase or s not in PURCHASE_STEPS]
    problems: list[str] = []
    warnings: list[str] = []
    step_results: dict[str, Any] = {}

    for step in steps:
        if step not in reached and not (step == "add_payment_info" and "purchase" in reached):
            problems.append(f"{step}: the test never reached this step (see errors)")
            continue
        # Shopify fires payment_info_submitted as the Pay button is clicked,
        # so those events land in the same window as the purchase.
        window = PURCHASE_STEPS if step == "add_payment_info" else (step,)
        in_step = [h for h in hits if h.step in window]
        results = {}
        for platform, event in expected_events(config, step).items():
            seen = [h for h in in_step if h.platform == platform and h.event == event]
            results[platform] = {"expected": event, "seen": len(seen)}
            if not seen:
                problems.append(f"{step}: {platform} '{event}' did not fire")
                continue
            if step in ("view_item", "add_to_cart", "begin_checkout", "purchase") and platform in ("meta", "tiktok"):
                first = seen[0]
                if first.value is None or not first.currency:
                    warnings.append(f"{step}: {platform} '{event}' fired without value/currency")
                if not first.content_ids:
                    warnings.append(f"{step}: {platform} '{event}' fired without product IDs (catalog matching fails)")
        step_results[step] = results

    # Double counting: the same purchase event sent more than once.
    purchase_hits = [h for h in hits if h.step == "purchase"]
    for (platform, event), count in Counter((h.platform, h.event) for h in purchase_hits).items():
        expected = expected_events(config, "purchase").get(platform)
        if event == expected and count > 1:
            ids = sorted({h.event_id for h in purchase_hits if h.platform == platform and h.event == event})
            problems.append(
                f"purchase: {platform} '{event}' fired {count} times (event IDs {ids}) - "
                "another app or leftover theme code is also sending it; conversions will double count"
            )

    # Purchase event IDs must match what the server-side events use.
    order_ids = sorted(
        {h.event_id.removeprefix("purchase-") for h in purchase_hits if h.event_id.startswith("purchase-")} - {""}
    )
    if purchase:
        for h in purchase_hits:
            if h.event == expected_events(config, "purchase").get(h.platform) and h.platform in ("meta", "tiktok"):
                if not h.event_id.startswith("purchase-"):
                    warnings.append(
                        f"purchase: {h.platform} event ID '{h.event_id}' isn't 'purchase-<order id>' - "
                        "server-side events won't deduplicate against it (is the latest pixel installed?)"
                    )

    # Pixels on the page that aren't in the config.
    configured = {
        platform: str(config["platforms"][platform].get(PIXEL_FIELD[platform], ""))
        for platform in PIXEL_FIELD
        if config["platforms"].get(platform, {}).get("enabled")
    }
    for platform, pixel_id in sorted({(h.platform, h.pixel_id) for h in hits if h.pixel_id and h.platform in PIXEL_FIELD}):
        if platform not in configured:
            warnings.append(f"{platform} pixel {pixel_id} is firing but {platform} isn't enabled in the config")
        elif pixel_id != configured[platform]:
            warnings.append(
                f"unexpected {platform} pixel {pixel_id} is firing (configured: {configured[platform]}) - "
                "an app or old theme code is installing a second pixel"
            )

    if purchase and config.get("capi", {}).get("enabled") and not any(h.platform == "capi_beacon" for h in hits):
        problems.append("purchase: the pixel never sent the server-side beacon to /collect")

    status = "fail" if problems else ("warn" if warnings else "pass")
    return {
        "status": status,
        "order_id": order_ids[0] if order_ids else None,
        "steps": step_results,
        "problems": problems,
        "warnings": sorted(set(warnings)),
        "hit_count": len(hits),
    }
