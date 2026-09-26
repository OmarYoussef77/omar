"""Build and send each platform's server-side purchase request.

Builders are pure functions (event + config + secrets -> HTTP request), so
payloads are unit-testable; send_request() does the HTTP.

Field names follow each platform's current public API docs. Always verify a
new setup with the platform's test mode first (Meta test_event_code, TikTok
test_event_code, Snap test_mode) before sending live traffic.
"""

from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Any, Mapping

from . import hashing as h
from .event import PurchaseEvent

DEFAULT_ENDPOINTS = {
    "meta": "https://graph.facebook.com",
    "tiktok": "https://business-api.tiktok.com",
    "snapchat": "https://tr.snapchat.com",
    "linkedin": "https://api.linkedin.com",
    "google_ads": "https://googleads.googleapis.com",
    "google_oauth": "https://oauth2.googleapis.com/token",
}

SECRET_ENV = {
    "shopify_webhook_secret": "SHOPIFY_WEBHOOK_SECRET",
    "shopify_admin_token": "SHOPIFY_ADMIN_TOKEN",
    "meta": "META_CAPI_ACCESS_TOKEN",
    "tiktok": "TIKTOK_EVENTS_ACCESS_TOKEN",
    "snapchat": "SNAP_CAPI_ACCESS_TOKEN",
    "linkedin": "LINKEDIN_CAPI_ACCESS_TOKEN",
    "google_ads_developer_token": "GOOGLE_ADS_DEVELOPER_TOKEN",
    "google_ads_client_id": "GOOGLE_ADS_CLIENT_ID",
    "google_ads_client_secret": "GOOGLE_ADS_CLIENT_SECRET",
    "google_ads_refresh_token": "GOOGLE_ADS_REFRESH_TOKEN",
    "health_report_token": "HEALTH_REPORT_TOKEN",
    "health_alert_webhook_url": "HEALTH_ALERT_WEBHOOK_URL",
}


def load_secrets(env: Mapping[str, str] = os.environ) -> dict[str, str]:
    return {key: env.get(var, "") for key, var in SECRET_ENV.items()}


@dataclass
class Request:
    url: str
    headers: dict[str, str]
    body: dict[str, Any]


class SkipPlatform(Exception):
    """Platform can't be sent for this order (missing token, config, IDs)."""


def _compact(data: dict[str, Any]) -> dict[str, Any]:
    return {k: v for k, v in data.items() if v not in (None, "", [], {})}


def _hashed_list(value: str | None) -> list[str] | None:
    hashed = h.sha256(value)
    return [hashed] if hashed else None


def _fbc(event: PurchaseEvent) -> str | None:
    if event.cookies.get("_fbc"):
        return event.cookies["_fbc"]
    if event.click_ids.get("fbclid"):
        return f"fb.1.{event.event_time * 1000}.{event.click_ids['fbclid']}"
    return None


# --- Meta Conversions API --------------------------------------------------


def build_meta(event: PurchaseEvent, config: dict, secrets: dict, base: str) -> Request:
    token = secrets.get("meta")
    if not token:
        raise SkipPlatform("META_CAPI_ACCESS_TOKEN not set")
    pixel_id = config["platforms"]["meta"]["pixel_id"]
    capi = config["capi"]["meta"]
    body: dict[str, Any] = {
        "data": [
            {
                "event_name": "Purchase",
                "event_time": event.event_time,
                "event_id": event.event_id,
                "action_source": "website",
                "event_source_url": event.source_url,
                "user_data": _compact(
                    {
                        "em": _hashed_list(h.email(event.email)),
                        "ph": _hashed_list(h.phone_digits(event.phone)),
                        "fn": _hashed_list(h.name(event.first_name)),
                        "ln": _hashed_list(h.name(event.last_name)),
                        "ct": _hashed_list(h.city(event.city)),
                        "st": _hashed_list(h.state(event.state)),
                        "zp": _hashed_list(h.zip_code(event.zip, event.country)),
                        "country": _hashed_list(h.country(event.country)),
                        "external_id": _hashed_list(event.external_id),
                        "client_ip_address": event.ip,
                        "client_user_agent": event.user_agent,
                        "fbp": event.cookies.get("_fbp"),
                        "fbc": _fbc(event),
                    }
                ),
                "custom_data": {
                    "currency": event.currency,
                    "value": event.value,
                    "order_id": event.order_id,
                    "content_type": "product",
                    "content_ids": event.content_ids,
                    "contents": [{"id": i.id, "quantity": i.quantity, "item_price": i.price} for i in event.items],
                    "num_items": event.num_items,
                },
            }
        ],
        "access_token": token,
    }
    if capi.get("test_event_code"):
        body["test_event_code"] = capi["test_event_code"]
    return Request(f"{base}/{capi['api_version']}/{pixel_id}/events", {}, body)


# --- TikTok Events API -----------------------------------------------------


def build_tiktok(event: PurchaseEvent, config: dict, secrets: dict, base: str) -> Request:
    token = secrets.get("tiktok")
    if not token:
        raise SkipPlatform("TIKTOK_EVENTS_ACCESS_TOKEN not set")
    body: dict[str, Any] = {
        "event_source": "web",
        "event_source_id": config["platforms"]["tiktok"]["pixel_id"],
        "data": [
            {
                "event": "CompletePayment",
                "event_time": event.event_time,
                "event_id": event.event_id,
                "user": _compact(
                    {
                        "email": h.sha256(h.email(event.email)),
                        "phone": h.sha256(h.phone_e164(event.phone)),
                        "external_id": h.sha256(event.external_id),
                        "ip": event.ip,
                        "user_agent": event.user_agent,
                        "ttclid": event.click_ids.get("ttclid") or event.cookies.get("ttclid"),
                        "ttp": event.cookies.get("_ttp"),
                    }
                ),
                "page": _compact({"url": event.source_url}),
                "properties": {
                    "currency": event.currency,
                    "value": event.value,
                    "order_id": event.order_id,
                    "content_type": "product",
                    "contents": [
                        {"content_id": i.id, "content_name": i.name, "quantity": i.quantity, "price": i.price}
                        for i in event.items
                    ],
                },
            }
        ],
    }
    test_code = config["capi"]["tiktok"].get("test_event_code")
    if test_code:
        body["test_event_code"] = test_code
    return Request(f"{base}/open_api/v1.3/event/track/", {"Access-Token": token}, body)


# --- Snap Conversions API (v3) --------------------------------------------


def build_snapchat(event: PurchaseEvent, config: dict, secrets: dict, base: str) -> Request:
    token = secrets.get("snapchat")
    if not token:
        raise SkipPlatform("SNAP_CAPI_ACCESS_TOKEN not set")
    pixel_id = config["platforms"]["snapchat"]["pixel_id"]
    path = "events/validate" if config["capi"]["snapchat"].get("test_mode") else "events"
    body = {
        "data": [
            {
                "event_name": "PURCHASE",
                "event_time": event.event_time,
                # Must equal the browser tag's client_dedup_id.
                "event_id": event.event_id,
                "action_source": "WEB",
                "event_source_url": event.source_url,
                "user_data": _compact(
                    {
                        "em": _hashed_list(h.email(event.email)),
                        "ph": _hashed_list(h.phone_digits(event.phone)),
                        "fn": _hashed_list(h.name(event.first_name)),
                        "ln": _hashed_list(h.name(event.last_name)),
                        "ct": _hashed_list(h.city(event.city)),
                        "st": _hashed_list(h.state(event.state)),
                        "zp": _hashed_list(h.zip_code(event.zip, event.country)),
                        "country": _hashed_list(h.country(event.country)),
                        "external_id": _hashed_list(event.external_id),
                        "client_ip_address": event.ip,
                        "client_user_agent": event.user_agent,
                        "sc_click_id": event.click_ids.get("ScCid"),
                        "sc_cookie1": event.cookies.get("_scid"),
                    }
                ),
                "custom_data": {
                    "currency": event.currency,
                    "value": event.value,
                    "order_id": event.order_id,
                    "content_ids": event.content_ids,
                    "num_items": event.num_items,
                    "contents": [{"id": i.id, "quantity": i.quantity, "item_price": i.price} for i in event.items],
                },
            }
        ]
    }
    query = urllib.parse.urlencode({"access_token": token})
    return Request(f"{base}/v3/{pixel_id}/{path}?{query}", {}, body)


# --- LinkedIn Conversions API ---------------------------------------------


def build_linkedin(event: PurchaseEvent, config: dict, secrets: dict, base: str) -> Request:
    token = secrets.get("linkedin")
    if not token:
        raise SkipPlatform("LINKEDIN_CAPI_ACCESS_TOKEN not set")
    conversion_id = (config["platforms"]["linkedin"].get("conversions") or {}).get("purchase")
    if not conversion_id:
        raise SkipPlatform("platforms.linkedin.conversions.purchase not set")
    user_ids = []
    if h.email(event.email):
        user_ids.append({"idType": "SHA256_EMAIL", "idValue": h.sha256(h.email(event.email))})
    li_fat_id = event.click_ids.get("li_fat_id") or event.cookies.get("li_fat_id")
    if li_fat_id:
        user_ids.append({"idType": "LINKEDIN_FIRST_PARTY_ADS_TRACKING_UUID", "idValue": li_fat_id})
    if not user_ids:
        raise SkipPlatform("no email or li_fat_id to match the user on")
    user: dict[str, Any] = {"userIds": user_ids}
    if event.first_name and event.last_name:
        user["userInfo"] = _compact(
            {"firstName": event.first_name, "lastName": event.last_name, "countryCode": event.country}
        )
    body = {
        "conversion": f"urn:lla:llaPartnerConversion:{conversion_id}",
        "conversionHappenedAt": event.event_time * 1000,
        "conversionValue": {"currencyCode": event.currency, "amount": f"{event.value:.2f}"},
        "eventId": event.event_id,
        "user": user,
    }
    headers = {
        "Authorization": f"Bearer {token}",
        "LinkedIn-Version": str(config["capi"]["linkedin"]["api_version"]),
        "X-Restli-Protocol-Version": "2.0.0",
    }
    return Request(f"{base}/rest/conversionEvents", headers, body)


# --- Google Ads enhanced conversions (conversion adjustments) -------------


def build_google_ads(event: PurchaseEvent, config: dict, secrets: dict, base: str, access_token: str) -> Request:
    ads = config["capi"]["google_ads"]
    customer_id = str(ads.get("customer_id", "")).replace("-", "")
    identifiers: list[dict[str, Any]] = []
    if h.google_email(event.email):
        identifiers.append({"hashedEmail": h.sha256(h.google_email(event.email))})
    if h.phone_e164(event.phone):
        identifiers.append({"hashedPhoneNumber": h.sha256(h.phone_e164(event.phone))})
    if event.first_name and event.last_name and event.country and event.zip:
        identifiers.append(
            {
                "addressInfo": _compact(
                    {
                        "hashedFirstName": h.sha256(h.name(event.first_name)),
                        "hashedLastName": h.sha256(h.name(event.last_name)),
                        "countryCode": event.country.upper(),
                        "postalCode": event.zip,
                    }
                )
            }
        )
    if not identifiers:
        raise SkipPlatform("no email, phone or address to enhance the conversion with")
    adjustment: dict[str, Any] = {
        "conversionAction": f"customers/{customer_id}/conversionActions/{ads['conversion_action_id']}",
        "adjustmentType": "ENHANCEMENT",
        # Must equal the browser conversion tag's transaction ID.
        "orderId": event.order_id,
        "userIdentifiers": identifiers[:5],
    }
    if event.user_agent:
        adjustment["userAgent"] = event.user_agent
    headers = {
        "Authorization": f"Bearer {access_token}",
        "developer-token": secrets["google_ads_developer_token"],
    }
    if ads.get("login_customer_id"):
        headers["login-customer-id"] = str(ads["login_customer_id"]).replace("-", "")
    return Request(
        f"{base}/{ads['api_version']}/customers/{customer_id}:uploadConversionAdjustments",
        headers,
        {"conversionAdjustments": [adjustment], "partialFailure": True},
    )


# --- HTTP ------------------------------------------------------------------


class SendError(Exception):
    def __init__(self, message: str, retryable: bool):
        super().__init__(message)
        self.retryable = retryable


def send_request(request: Request, timeout: float = 20.0) -> dict[str, Any]:
    data = json.dumps(request.body).encode()
    headers = {"Content-Type": "application/json", **request.headers}
    http_request = urllib.request.Request(request.url, data=data, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(http_request, timeout=timeout) as response:
            text = response.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")[:500]
        retryable = exc.code == 429 or exc.code >= 500
        raise SendError(f"HTTP {exc.code}: {detail}", retryable) from exc
    except (urllib.error.URLError, TimeoutError) as exc:
        raise SendError(f"network error: {exc}", True) from exc
    parsed: dict[str, Any] = json.loads(text) if text.strip().startswith("{") else {"raw": text}
    _raise_for_body_errors(parsed)
    return parsed


def _raise_for_body_errors(body: dict[str, Any]) -> None:
    """Some APIs report failures inside an HTTP 200 body."""
    if isinstance(body.get("code"), int) and body["code"] != 0:  # TikTok
        raise SendError(f"TikTok error {body['code']}: {body.get('message')}", False)
    if body.get("partialFailureError"):  # Google Ads
        raise SendError(f"Google Ads partial failure: {body['partialFailureError'].get('message')}", False)


class GoogleTokenCache:
    def __init__(self, endpoint: str):
        self.endpoint = endpoint
        self._token = ""
        self._expires = 0.0

    def get(self, secrets: dict[str, str]) -> str:
        if self._token and time.time() < self._expires - 60:
            return self._token
        form = urllib.parse.urlencode(
            {
                "grant_type": "refresh_token",
                "client_id": secrets["google_ads_client_id"],
                "client_secret": secrets["google_ads_client_secret"],
                "refresh_token": secrets["google_ads_refresh_token"],
            }
        ).encode()
        request = urllib.request.Request(self.endpoint, data=form, method="POST")
        try:
            with urllib.request.urlopen(request, timeout=20) as response:
                payload = json.loads(response.read())
        except urllib.error.HTTPError as exc:
            raise SendError(f"Google OAuth HTTP {exc.code}", exc.code >= 500) from exc
        self._token = payload["access_token"]
        self._expires = time.time() + int(payload.get("expires_in", 3600))
        return self._token


class Dispatcher:
    """Builds and sends one platform's request for one event."""

    def __init__(self, config: dict, secrets: dict, endpoints: dict[str, str] | None = None):
        self.config = config
        self.secrets = secrets
        self.endpoints = {**DEFAULT_ENDPOINTS, **(endpoints or {})}
        self._google_tokens = GoogleTokenCache(self.endpoints["google_oauth"])

    def build(self, platform: str, event: PurchaseEvent) -> Request:
        base = self.endpoints[platform]
        if platform == "meta":
            return build_meta(event, self.config, self.secrets, base)
        if platform == "tiktok":
            return build_tiktok(event, self.config, self.secrets, base)
        if platform == "snapchat":
            return build_snapchat(event, self.config, self.secrets, base)
        if platform == "linkedin":
            return build_linkedin(event, self.config, self.secrets, base)
        if platform == "google_ads":
            required = ("google_ads_developer_token", "google_ads_client_id", "google_ads_client_secret", "google_ads_refresh_token")
            missing = [SECRET_ENV[k] for k in required if not self.secrets.get(k)]
            if missing:
                raise SkipPlatform(f"missing {', '.join(missing)}")
            ads = self.config["capi"]["google_ads"]
            if not (ads.get("customer_id") and ads.get("conversion_action_id")):
                raise SkipPlatform("capi.google_ads.customer_id / conversion_action_id not set")
            return build_google_ads(event, self.config, self.secrets, base, self._google_tokens.get(self.secrets))
        raise SkipPlatform(f"unknown platform {platform}")

    def send(self, platform: str, event: PurchaseEvent) -> dict[str, Any]:
        return send_request(self.build(platform, event))
