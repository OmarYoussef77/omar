"""Tracking configuration: store details, GTM container, and per-platform IDs.

The config is a YAML file (see tracking.example.yaml). The agent reads and
edits it through tools, so every ID it uses is one the user supplied.
"""

from __future__ import annotations

import copy
import re
from pathlib import Path
from typing import Any

import yaml

PLATFORMS = ("ga4", "google_ads", "meta", "tiktok", "snapchat", "linkedin")

# Canonical events follow the GA4 ecommerce schema; the Shopify pixel pushes
# these names to the dataLayer and every platform maps from them.
CANONICAL_EVENTS = (
    "page_view",
    "view_item_list",
    "view_item",
    "search",
    "add_to_cart",
    "view_cart",
    "begin_checkout",
    "add_shipping_info",
    "add_payment_info",
    "purchase",
)

ITEM_ID_SOURCES = ("variant_id", "product_id", "sku", "shopify_feed")

DEFAULT_CONFIG: dict[str, Any] = {
    "store": {
        "name": "",
        "domain": "",
        "currency": "USD",
        # Must match the IDs in your product catalogs/feeds (Meta, TikTok,
        # Snap, Google Merchant Center) or dynamic ads won't match products.
        # shopify_feed = "shopify_<COUNTRY>_<productId>_<variantId>", the ID
        # format Shopify's Google & YouTube and Facebook & Instagram apps use.
        "item_id_source": "variant_id",
        "feed_country": "US",
    },
    "gtm": {
        "container_public_id": "",
        "account_id": "",
        "container_id": "",
        "credentials_file": "",
    },
    "platforms": {
        "ga4": {"enabled": False, "measurement_id": ""},
        "google_ads": {"enabled": False, "conversion_id": "", "conversions": {}},
        "meta": {"enabled": False, "pixel_id": ""},
        "tiktok": {"enabled": False, "pixel_id": ""},
        "snapchat": {"enabled": False, "pixel_id": ""},
        "linkedin": {"enabled": False, "partner_id": "", "conversions": {}},
    },
}

_ID_PATTERNS: dict[tuple[str, str], tuple[str, str]] = {
    ("gtm", "container_public_id"): (r"GTM-[A-Z0-9]{4,10}", "GTM-XXXXXXX"),
    ("ga4", "measurement_id"): (r"G-[A-Z0-9]{6,12}", "G-XXXXXXXXXX"),
    ("google_ads", "conversion_id"): (r"AW-\d{6,12}", "AW-123456789"),
    ("meta", "pixel_id"): (r"\d{15,16}", "a 15-16 digit number"),
    ("tiktok", "pixel_id"): (r"[A-Z0-9]{20}", "20 uppercase letters/digits, e.g. C4ABCDEFGHIJKLMNOPQR"),
    ("snapchat", "pixel_id"): (
        r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}",
        "a UUID",
    ),
    ("linkedin", "partner_id"): (r"\d{5,10}", "a numeric partner ID"),
}


def _deep_merge(base: dict, override: dict) -> dict:
    out = copy.deepcopy(base)
    for key, value in (override or {}).items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = _deep_merge(out[key], value)
        else:
            out[key] = value
    return out


def load_config(path: str | Path) -> dict[str, Any]:
    path = Path(path)
    data = yaml.safe_load(path.read_text()) if path.exists() else {}
    return _deep_merge(DEFAULT_CONFIG, data or {})


def save_config(config: dict[str, Any], path: str | Path) -> None:
    Path(path).write_text(yaml.safe_dump(config, sort_keys=False))


def set_value(config: dict[str, Any], dotted_path: str, value: Any) -> dict[str, Any]:
    """Set e.g. 'platforms.meta.pixel_id' in place and return the config."""
    keys = dotted_path.split(".")
    if keys[0] not in DEFAULT_CONFIG:
        raise KeyError(f"Unknown top-level section '{keys[0]}'. Expected one of {list(DEFAULT_CONFIG)}.")
    if keys[0] == "platforms" and len(keys) > 1 and keys[1] not in PLATFORMS:
        raise KeyError(f"Unknown platform '{keys[1]}'. Expected one of {list(PLATFORMS)}.")
    node = config
    for key in keys[:-1]:
        node = node.setdefault(key, {})
        if not isinstance(node, dict):
            raise KeyError(f"'{key}' in '{dotted_path}' is not a section")
    node[keys[-1]] = value
    return config


def enabled_platforms(config: dict[str, Any]) -> list[str]:
    return [p for p in PLATFORMS if config["platforms"].get(p, {}).get("enabled")]


def validate_config(config: dict[str, Any]) -> dict[str, list[str]]:
    """Return {"errors": [...], "warnings": [...]}; no errors means ready to generate."""
    problems: list[str] = []
    warnings: list[str] = []

    store = config["store"]
    if store.get("item_id_source") not in ITEM_ID_SOURCES:
        problems.append(f"store.item_id_source must be one of {ITEM_ID_SOURCES}")
    if not re.fullmatch(r"[A-Z]{3}", str(store.get("currency", ""))):
        problems.append("store.currency must be a 3-letter ISO code, e.g. USD")

    gtm_id = config["gtm"].get("container_public_id", "")
    pattern, example = _ID_PATTERNS[("gtm", "container_public_id")]
    if not re.fullmatch(pattern, str(gtm_id)):
        problems.append(f"gtm.container_public_id '{gtm_id}' should look like {example}")

    platforms = enabled_platforms(config)
    if not platforms:
        problems.append("No platforms enabled (set platforms.<name>.enabled: true)")

    for platform in platforms:
        settings = config["platforms"][platform]
        for (section, field), (pattern, example) in _ID_PATTERNS.items():
            if section != platform:
                continue
            value = str(settings.get(field, ""))
            if not re.fullmatch(pattern, value):
                problems.append(f"platforms.{platform}.{field} '{value}' should be {example}")

        for event in (settings.get("conversions") or {}):
            if event not in CANONICAL_EVENTS:
                problems.append(
                    f"platforms.{platform}.conversions has unknown event '{event}'. "
                    f"Use one of {CANONICAL_EVENTS}"
                )

    ads = config["platforms"]["google_ads"]
    if ads.get("enabled") and not ads.get("conversions"):
        warnings.append(
            "platforms.google_ads.conversions is empty - only the Google tag and "
            "conversion linker will be created; add {purchase: <conversion label>} "
            "from Google Ads > Goals > Conversions to track conversions"
        )
    li = config["platforms"]["linkedin"]
    if li.get("enabled") and not li.get("conversions"):
        warnings.append(
            "platforms.linkedin.conversions is empty - Insight Tag page views will fire, "
            "but add {purchase: <conversion id>} to track purchases"
        )
    return {"errors": problems, "warnings": warnings}
