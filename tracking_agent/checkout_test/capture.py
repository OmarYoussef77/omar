"""Recognize tracking requests in browser network traffic.

Each platform's browser library sends events in its own wire format; this
turns a request (URL, method, body) into zero or more Hit records:
platform, event name, event ID, pixel ID, value, currency, product IDs.
"""

from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass, field
from typing import Any
from urllib.parse import parse_qsl, urlparse


@dataclass
class Hit:
    platform: str
    event: str
    pixel_id: str = ""
    event_id: str = ""
    value: float | None = None
    currency: str = ""
    content_ids: list[str] = field(default_factory=list)
    url: str = ""
    step: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _float(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _form(body: str | None) -> dict[str, str]:
    if not body:
        return {}
    if "Content-Disposition: form-data" in body:  # multipart
        return dict(re.findall(r'name="([^"]+)"\r?\n\r?\n(.*?)\r?\n--', body, re.S))
    if body.lstrip().startswith(("{", "[")):
        return {}
    return dict(parse_qsl(body, keep_blank_values=True))


def _json(body: str | None) -> Any:
    try:
        return json.loads(body or "")
    except ValueError:
        return None


def _ids(value: Any) -> list[str]:
    if isinstance(value, list):
        return [str(v) for v in value]
    if isinstance(value, str) and value.strip().startswith("["):
        parsed = _json(value)
        if isinstance(parsed, list):
            return [str(v) for v in parsed]
    if isinstance(value, str) and value:
        return [v for v in value.split(",") if v]
    return []


# --- per-platform parsers ----------------------------------------------------


def _meta(url: str, params: dict[str, str]) -> list[Hit]:
    if not params.get("ev"):
        return []
    return [
        Hit(
            "meta",
            params["ev"],
            pixel_id=params.get("id", ""),
            event_id=params.get("eid", ""),
            value=_float(params.get("cd[value]")),
            currency=params.get("cd[currency]", ""),
            content_ids=_ids(params.get("cd[content_ids]")),
            url=url,
        )
    ]


def _tiktok(url: str, body: Any) -> list[Hit]:
    events = body.get("batch") if isinstance(body, dict) and "batch" in body else [body]
    hits = []
    for event in events or []:
        if not isinstance(event, dict) or not event.get("event"):
            continue
        props = event.get("properties") or {}
        contents = props.get("contents") or []
        hits.append(
            Hit(
                "tiktok",
                event["event"],
                pixel_id=str(((event.get("context") or {}).get("pixel") or {}).get("code", "")),
                event_id=str(event.get("event_id", "")),
                value=_float(props.get("value")),
                currency=str(props.get("currency", "")),
                content_ids=[str(c.get("content_id")) for c in contents if isinstance(c, dict) and c.get("content_id")],
                url=url,
            )
        )
    return hits


def _snap(url: str, params: dict[str, str]) -> list[Hit]:
    if not params.get("ev"):
        return []
    return [
        Hit(
            "snapchat",
            params["ev"],
            pixel_id=params.get("pid", ""),
            event_id=params.get("client_dedup_id") or params.get("cdid", ""),
            value=_float(params.get("e_pr") or params.get("price")),
            currency=params.get("e_cur") or params.get("currency", ""),
            content_ids=_ids(params.get("e_iids") or params.get("item_ids")),
            url=url,
        )
    ]


def _linkedin(url: str, params: dict[str, str]) -> list[Hit]:
    if not params.get("pid"):
        return []
    conversion = params.get("conversionId")
    return [
        Hit(
            "linkedin",
            f"conversion:{conversion}" if conversion else "pageview",
            pixel_id=params["pid"],
            event_id=params.get("eventId", ""),
            url=url,
        )
    ]


def _ga4(url: str, query: dict[str, str], body: str | None) -> list[Hit]:
    # One request can carry several events: query holds shared params, each
    # body line holds one event's params.
    lines = [dict(parse_qsl(line)) for line in (body or "").splitlines() if "en=" in line] or [{}]
    hits = []
    for line in lines:
        params = {**query, **line}
        if not params.get("en"):
            continue
        hits.append(
            Hit(
                "ga4",
                params["en"],
                pixel_id=params.get("tid", ""),
                event_id=params.get("ep.transaction_id", ""),
                value=_float(params.get("epn.value")),
                currency=params.get("cu", ""),
                url=url,
            )
        )
    return hits


def _google_ads(url: str, path: str, params: dict[str, str]) -> list[Hit]:
    match = re.search(r"/pagead/(?:1p-)?(conversion|viewthroughconversion)/(\d+)", path)
    if not match:
        return []
    kind, conversion_id = match.groups()
    label = params.get("label", "")
    event = f"conversion:{label}" if label and kind == "conversion" else ("conversion" if kind == "conversion" else "remarketing")
    return [
        Hit(
            "google_ads",
            event,
            pixel_id=f"AW-{conversion_id}",
            event_id=params.get("oid", ""),
            value=_float(params.get("value")),
            currency=params.get("currency_code", ""),
            url=url,
        )
    ]


def classify(url: str, method: str = "GET", body: str | None = None, collect_url: str = "") -> list[Hit]:
    parsed = urlparse(url)
    host, path = parsed.netloc.lower(), parsed.path
    query = dict(parse_qsl(parsed.query, keep_blank_values=True))

    if collect_url and url.split("?")[0].rstrip("/") == collect_url.rstrip("/"):
        data = _json(body) or {}
        return [
            Hit(
                "capi_beacon",
                "beacon",
                event_id=f"purchase-{data.get('order_id', '')}",
                url=url,
            )
        ]
    if host.endswith("facebook.com") and path.rstrip("/") == "/tr":
        return _meta(url, {**query, **_form(body)})
    if host == "analytics.tiktok.com" and path.startswith("/api/v2/pixel"):
        return _tiktok(url, _json(body))
    if host.endswith("tr.snapchat.com") and path in ("/p", "/cm/p", "/gateway/p"):
        return _snap(url, {**query, **_form(body)})
    if host == "px.ads.linkedin.com" and path.startswith(("/collect", "/wa")):
        return _linkedin(url, {**query, **_form(body)})
    if (host.endswith("google-analytics.com") or host == "analytics.google.com") and path.endswith("/g/collect"):
        return _ga4(url, query, body)
    if host in ("www.googleadservices.com", "googleads.g.doubleclick.net", "www.google.com") and "/pagead/" in path:
        return _google_ads(url, path, query)
    return []
