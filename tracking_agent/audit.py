"""Detect tracking already installed on a storefront.

Double-firing is the most common tracking bug on Shopify: a native sales
channel app (Facebook & Instagram, TikTok, Snapchat, Google & YouTube)
already sends the pixel, then GTM sends it again and conversions double.
This audit finds existing tags so the agent can pick one source per platform.
"""

from __future__ import annotations

import re
import urllib.request
from typing import Any

from .config import enabled_platforms

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/128.0 Safari/537.36"
)

# platform -> list of (description, regex with one capture group for the ID)
DETECTORS: dict[str, list[tuple[str, str]]] = {
    "gtm": [("GTM container", r"(GTM-[A-Z0-9]{4,10})")],
    "ga4": [("GA4 measurement ID", r"\b(G-[A-Z0-9]{6,12})\b")],
    "google_ads": [("Google Ads tag", r"\b(AW-\d{6,12})\b")],
    "meta": [
        ("fbq init", r"fbq\(\s*['\"]init['\"]\s*,\s*['\"]?(\d{15,16})"),
        ("Meta pixel in Shopify app config", r"[\"']pixel_?[iI]d[\"']\s*:\s*[\"'](\d{15,16})[\"']"),
    ],
    "tiktok": [
        ("ttq.load", r"ttq\.load\(\s*['\"]([A-Z0-9]{20})['\"]"),
        ("TikTok pixel in Shopify app config", r"[\"']pixel_?[cC]ode[\"']\s*:\s*[\"']([A-Z0-9]{20})[\"']"),
    ],
    "snapchat": [
        (
            "snaptr init / Snap app config",
            r"(?:snaptr\(\s*['\"]init['\"]\s*,\s*|[\"']pixel_?[iI]d[\"']\s*:\s*)['\"]"
            r"([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})",
        )
    ],
    "linkedin": [("LinkedIn partner ID", r"_linkedin_partner_id\s*=\s*['\"]?(\d{5,10})")],
}

SHOPIFY_MARKERS = {
    "is_shopify": r"cdn\.shopify\.com|Shopify\.theme",
    "web_pixels_manager": r"web-pixels-manager|webPixelsManager",
    "checkout_extensibility_hint": r"checkout\.shopify\.com|/checkouts/",
}


def fetch(url: str, timeout: float = 20.0) -> str:
    if not url.startswith(("http://", "https://")):
        url = "https://" + url
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return response.read().decode("utf-8", errors="replace")


def detect(html: str) -> dict[str, Any]:
    found: dict[str, list[dict[str, str]]] = {}
    for platform, patterns in DETECTORS.items():
        hits: dict[str, str] = {}
        for label, pattern in patterns:
            for match in re.finditer(pattern, html):
                hits.setdefault(match.group(1), label)
        if hits:
            found[platform] = [{"id": id_, "source": label} for id_, label in hits.items()]
    shopify = {name: bool(re.search(pattern, html)) for name, pattern in SHOPIFY_MARKERS.items()}
    return {"installed": found, "shopify": shopify}


def compare_with_config(detected: dict[str, Any], config: dict[str, Any]) -> list[str]:
    """Warnings about conflicts between what's live and what we plan to deploy."""
    warnings: list[str] = []
    installed = detected["installed"]
    planned_gtm = config["gtm"].get("container_public_id")
    live_gtm = {hit["id"] for hit in installed.get("gtm", [])}
    if live_gtm and planned_gtm and planned_gtm not in live_gtm:
        warnings.append(
            f"Storefront already loads GTM {sorted(live_gtm)}; the plan uses {planned_gtm}. "
            "Remove the old container from the theme or you will run two containers."
        )
    if planned_gtm in live_gtm:
        warnings.append(
            f"{planned_gtm} is hard-coded in the theme. Once it also loads via the custom pixel, "
            "remove it from theme.liquid so events aren't sent twice."
        )

    id_field = {
        "ga4": "measurement_id",
        "google_ads": "conversion_id",
        "meta": "pixel_id",
        "tiktok": "pixel_id",
        "snapchat": "pixel_id",
        "linkedin": "partner_id",
    }
    for platform in enabled_platforms(config):
        hits = installed.get(platform, [])
        if not hits:
            continue
        planned_id = str(config["platforms"][platform].get(id_field[platform], ""))
        same = [h for h in hits if h["id"] == planned_id]
        if same:
            warnings.append(
                f"{platform}: {planned_id} is already firing on the storefront ({same[0]['source']}). "
                "Deploying it via GTM too will double-count. Pick ONE source: keep the native "
                "Shopify app (usually better - it includes server-side CAPI) and disable the GTM tags "
                f"for {platform}, or disconnect the app's pixel and let GTM own it."
            )
        else:
            warnings.append(
                f"{platform}: a different ID {[h['id'] for h in hits]} is live on the storefront "
                f"(planned: {planned_id}). Confirm which one is correct before deploying."
            )
    return warnings


def audit_url(url: str, config: dict[str, Any] | None = None) -> dict[str, Any]:
    html = fetch(url)
    result = detect(html)
    result["url"] = url
    result["note"] = (
        "Only the storefront HTML was scanned. App pixels, custom pixels and checkout "
        "tracking don't all appear in page source - also review Settings > Customer events "
        "and each sales channel app in Shopify admin."
    )
    if config is not None:
        result["warnings"] = compare_with_config(result, config)
    return result
