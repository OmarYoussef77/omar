"""Normalize and SHA-256 hash customer data the way each platform expects.

Every platform matches on the hash, so normalization must be exact:
"John@Example.com " and "john@example.com" must hash identically.
"""

from __future__ import annotations

import hashlib
import re
import unicodedata


def sha256(value: str | None) -> str | None:
    if not value:
        return None
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def email(value: str | None) -> str | None:
    value = (value or "").strip().lower()
    return value if "@" in value else None


def google_email(value: str | None) -> str | None:
    """Google Ads also drops dots from the local part of Gmail addresses."""
    value = email(value)
    if not value:
        return None
    local, _, domain = value.partition("@")
    if domain in ("gmail.com", "googlemail.com"):
        local = local.replace(".", "")
    return f"{local}@{domain}"


def phone_e164(value: str | None) -> str | None:
    """'+1 (555) 555-0123' -> '+15555550123'. Shopify stores E.164 already;
    numbers without a country code can't be fixed reliably, so they're dropped."""
    raw = (value or "").strip()
    if raw.startswith("00"):
        raw = "+" + raw[2:]
    digits = re.sub(r"\D", "", raw)
    if not raw.startswith("+") or not 8 <= len(digits) <= 15:
        return None
    return "+" + digits


def phone_digits(value: str | None) -> str | None:
    """Meta and Snap: E.164 without the '+'."""
    e164 = phone_e164(value)
    return e164[1:] if e164 else None


def _strip_accents(value: str) -> str:
    return "".join(c for c in unicodedata.normalize("NFKD", value) if not unicodedata.combining(c))


def name(value: str | None) -> str | None:
    value = re.sub(r"[^\w]", "", (value or "").strip().lower())
    return value or None


def city(value: str | None) -> str | None:
    value = re.sub(r"[^a-z]", "", _strip_accents((value or "").lower()))
    return value or None


def state(value: str | None) -> str | None:
    value = re.sub(r"[^a-z]", "", (value or "").lower())
    return value or None


def zip_code(value: str | None, country_code: str | None = None) -> str | None:
    value = re.sub(r"[\s-]", "", (value or "").lower())
    if (country_code or "").upper() == "US":
        value = value[:5]
    return value or None


def country(value: str | None) -> str | None:
    value = (value or "").strip().lower()
    return value if re.fullmatch(r"[a-z]{2}", value) else None
