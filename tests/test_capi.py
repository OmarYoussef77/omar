import base64
import copy
import hashlib
import hmac
import io
import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from wsgiref.util import setup_testing_defaults

import pytest

from tracking_agent.capi import hashing as h
from tracking_agent.capi.event import from_shopify_order, order_number
from tracking_agent.capi.platforms import (
    Dispatcher,
    SendError,
    SkipPlatform,
    build_google_ads,
    build_linkedin,
    build_meta,
    build_snapchat,
    build_tiktok,
)
from tracking_agent.capi.server import CapiApp, clean_beacon, verify_shopify_hmac
from tracking_agent.capi.store import Store
from tracking_agent.capi.worker import process_due

SECRET = "shpss_test_secret"

ORDER = {
    "id": 9001,
    "admin_graphql_api_id": "gid://shopify/Order/9001",
    "checkout_token": "tok",
    "email": " Buyer@Example.com ",
    "phone": None,
    "browser_ip": "203.0.113.7",
    "client_details": {"user_agent": "Mozilla/5.0 Test"},
    "landing_site": "/products/tee?fbclid=FBCLID123&ttclid=TTCLID456&ScCid=SNAP789&li_fat_id=LIFAT&gclid=GCLID",
    "order_status_url": "https://example-store.com/orders/abc",
    "processed_at": "2026-09-26T12:00:00Z",
    "currency": "USD",
    "presentment_currency": "USD",
    "total_price": "55.00",
    "total_price_set": {"presentment_money": {"amount": "55.00", "currency_code": "USD"}},
    "customer": {"id": 777, "email": "buyer@example.com", "first_name": "Jane", "last_name": "Doe"},
    "billing_address": {
        "first_name": "Jane",
        "last_name": "O'Doe",
        "city": "San José",
        "province_code": "CA",
        "zip": "95112-1234",
        "country_code": "US",
        "phone": "+1 (555) 555-0123",
    },
    "line_items": [
        {
            "product_id": 111,
            "variant_id": 4444,
            "sku": "TEE-RED-M",
            "title": "Classic Tee",
            "quantity": 2,
            "price": "25.00",
            "price_set": {"presentment_money": {"amount": "25.00", "currency_code": "USD"}},
        }
    ],
}
BEACON = {
    "marketing_allowed": True,
    "page_url": "https://example-store.com/checkouts/c/abc/thank-you",
    "user_agent": "Mozilla/5.0 Beacon",
    "cookies": {"_fbp": "fb.1.111.222", "_ttp": "ttp-abc", "_scid": "scid-xyz"},
}


@pytest.fixture
def capi_config(config):
    config = copy.deepcopy(config)
    config["capi"]["enabled"] = True
    config["capi"]["server_url"] = "https://capi.example.com"
    config["capi"]["google_ads"].update(customer_id="123-456-7890", conversion_action_id="555", login_customer_id="")
    return config


SECRETS = {
    "shopify_webhook_secret": SECRET,
    "meta": "meta-token",
    "tiktok": "tiktok-token",
    "snapchat": "snap-token",
    "linkedin": "li-token",
    "google_ads_developer_token": "dev-token",
    "google_ads_client_id": "cid",
    "google_ads_client_secret": "csecret",
    "google_ads_refresh_token": "refresh",
}


def _event(config, beacon=BEACON):
    return from_shopify_order(ORDER, beacon, item_id_source="shopify_feed", feed_country="US")


def sha(value):
    return hashlib.sha256(value.encode()).hexdigest()


# --- normalization / hashing -----------------------------------------------


def test_hashing_normalization():
    assert h.email(" Buyer@Example.COM ") == "buyer@example.com"
    assert h.google_email("J.Doe@GMail.com") == "jdoe@gmail.com"
    assert h.google_email("j.doe@example.com") == "j.doe@example.com"
    assert h.phone_e164("+1 (555) 555-0123") == "+15555550123"
    assert h.phone_digits("+1 (555) 555-0123") == "15555550123"
    assert h.phone_e164("555-0123") is None  # no country code: don't guess
    assert h.phone_e164("0044 20 7946 0958") == "+442079460958"
    assert h.name(" O'Doe ") == "odoe"
    assert h.city("San José") == "sanjose"
    assert h.zip_code("95112-1234", "US") == "95112"
    assert h.zip_code("SW1A 1AA", "GB") == "sw1a1aa"
    assert h.country("US") == "us"
    assert h.sha256("buyer@example.com") == sha("buyer@example.com")
    assert h.sha256(None) is None


def test_order_number_formats():
    assert order_number(9001) == "9001"
    assert order_number("gid://shopify/Order/9001") == "9001"
    assert order_number("gid://shopify/OrderIdentity/9001") == "9001"
    assert order_number(None) == ""


# --- event mapping ---------------------------------------------------------


def test_event_from_order(config):
    event = _event(config)
    assert event.order_id == "9001"
    assert event.event_id == "purchase-9001"
    assert event.value == 55.0 and event.currency == "USD"
    assert event.content_ids == ["shopify_US_111_4444"] and event.num_items == 2
    assert event.click_ids["fbclid"] == "FBCLID123" and event.click_ids["ScCid"] == "SNAP789"
    assert event.cookies["_fbp"] == "fb.1.111.222"
    assert event.ip == "203.0.113.7"
    assert event.user_agent == "Mozilla/5.0 Test"  # order's client_details wins over beacon
    assert event.source_url.endswith("/thank-you")
    assert event.phone == "+1 (555) 555-0123"  # falls back to billing address
    assert event.marketing_allowed is True
    assert from_shopify_order(ORDER, None).marketing_allowed is None


# --- payloads --------------------------------------------------------------


def test_meta_payload(capi_config):
    request = build_meta(_event(capi_config), capi_config, SECRETS, "https://graph.facebook.com")
    assert request.url == "https://graph.facebook.com/v23.0/1234567890123456/events"
    data = request.body["data"][0]
    assert data["event_name"] == "Purchase" and data["event_id"] == "purchase-9001"
    assert data["action_source"] == "website"
    user = data["user_data"]
    assert user["em"] == [sha("buyer@example.com")]
    assert user["ph"] == [sha("15555550123")]
    assert user["ct"] == [sha("sanjose")] and user["zp"] == [sha("95112")]
    assert user["fbp"] == "fb.1.111.222"
    assert user["fbc"] == f"fb.1.{data['event_time'] * 1000}.FBCLID123"
    assert data["custom_data"]["content_ids"] == ["shopify_US_111_4444"]
    assert data["custom_data"]["order_id"] == "9001"
    assert request.body["access_token"] == "meta-token"
    assert "test_event_code" not in request.body

    capi_config["capi"]["meta"]["test_event_code"] = "TEST123"
    assert build_meta(_event(capi_config), capi_config, SECRETS, "x").body["test_event_code"] == "TEST123"


def test_tiktok_payload(capi_config):
    request = build_tiktok(_event(capi_config), capi_config, SECRETS, "https://business-api.tiktok.com")
    assert request.url == "https://business-api.tiktok.com/open_api/v1.3/event/track/"
    assert request.headers == {"Access-Token": "tiktok-token"}
    assert request.body["event_source_id"] == "C4ABCDEFGHIJKLMNOPQR"
    data = request.body["data"][0]
    assert data["event"] == "CompletePayment" and data["event_id"] == "purchase-9001"
    assert data["user"]["email"] == sha("buyer@example.com")
    assert data["user"]["phone"] == sha("+15555550123")  # TikTok hashes E.164 with '+'
    assert data["user"]["ttclid"] == "TTCLID456" and data["user"]["ttp"] == "ttp-abc"
    assert data["properties"]["contents"][0]["content_id"] == "shopify_US_111_4444"


def test_snap_payload(capi_config):
    request = build_snapchat(_event(capi_config), capi_config, SECRETS, "https://tr.snapchat.com")
    assert request.url.startswith("https://tr.snapchat.com/v3/0f1e2d3c-4b5a-6978-8a9b-0c1d2e3f4a5b/events?access_token=")
    data = request.body["data"][0]
    assert data["event_name"] == "PURCHASE" and data["event_id"] == "purchase-9001"
    assert data["user_data"]["sc_click_id"] == "SNAP789" and data["user_data"]["sc_cookie1"] == "scid-xyz"
    capi_config["capi"]["snapchat"]["test_mode"] = True
    assert "/events/validate?" in build_snapchat(_event(capi_config), capi_config, SECRETS, "x").url


def test_linkedin_payload(capi_config):
    request = build_linkedin(_event(capi_config), capi_config, SECRETS, "https://api.linkedin.com")
    assert request.url == "https://api.linkedin.com/rest/conversionEvents"
    assert request.headers["Authorization"] == "Bearer li-token"
    assert request.headers["LinkedIn-Version"] == "202509"
    body = request.body
    assert body["conversion"] == "urn:lla:llaPartnerConversion:12345678"
    assert body["eventId"] == "purchase-9001"
    assert body["conversionValue"] == {"currencyCode": "USD", "amount": "55.00"}
    assert {"idType": "SHA256_EMAIL", "idValue": sha("buyer@example.com")} in body["user"]["userIds"]
    assert {"idType": "LINKEDIN_FIRST_PARTY_ADS_TRACKING_UUID", "idValue": "LIFAT"} in body["user"]["userIds"]


def test_google_ads_payload(capi_config):
    request = build_google_ads(_event(capi_config), capi_config, SECRETS, "https://googleads.googleapis.com", "ya29.token")
    assert request.url == "https://googleads.googleapis.com/v22/customers/1234567890:uploadConversionAdjustments"
    assert request.headers["developer-token"] == "dev-token"
    assert request.headers["Authorization"] == "Bearer ya29.token"
    adjustment = request.body["conversionAdjustments"][0]
    assert adjustment["adjustmentType"] == "ENHANCEMENT"
    assert adjustment["orderId"] == "9001"  # equals the browser conversion tag's transaction ID
    assert adjustment["conversionAction"] == "customers/1234567890/conversionActions/555"
    assert {"hashedEmail": sha("buyer@example.com")} in adjustment["userIdentifiers"]
    assert {"hashedPhoneNumber": sha("+15555550123")} in adjustment["userIdentifiers"]
    assert request.body["partialFailure"] is True


def test_missing_tokens_skip_platform(capi_config):
    with pytest.raises(SkipPlatform, match="META_CAPI_ACCESS_TOKEN"):
        build_meta(_event(capi_config), capi_config, {}, "x")
    dispatcher = Dispatcher(capi_config, {**SECRETS, "google_ads_refresh_token": ""})
    with pytest.raises(SkipPlatform, match="GOOGLE_ADS_REFRESH_TOKEN"):
        dispatcher.build("google_ads", _event(capi_config))


# --- web server ------------------------------------------------------------


def _call(app, method, path, body=b"", headers=None):
    environ = {}
    setup_testing_defaults(environ)
    environ.update(REQUEST_METHOD=method, PATH_INFO=path, CONTENT_LENGTH=str(len(body)))
    environ["wsgi.input"] = io.BytesIO(body)
    environ.update(headers or {})
    captured = {}

    def start_response(status, response_headers):
        captured["status"] = int(status.split()[0])
        captured["headers"] = dict(response_headers)

    captured["body"] = b"".join(app(environ, start_response))
    return captured


def _signed(order):
    body = json.dumps(order).encode()
    signature = base64.b64encode(hmac.new(SECRET.encode(), body, hashlib.sha256).digest()).decode()
    return body, {"HTTP_X_SHOPIFY_HMAC_SHA256": signature}


@pytest.fixture
def app(tmp_path, capi_config):
    return CapiApp(capi_config, Store(str(tmp_path / "capi.sqlite3")), SECRETS)


def test_hmac_verification():
    body, headers = _signed(ORDER)
    assert verify_shopify_hmac(body, headers["HTTP_X_SHOPIFY_HMAC_SHA256"], SECRET)
    assert not verify_shopify_hmac(body + b" ", headers["HTTP_X_SHOPIFY_HMAC_SHA256"], SECRET)
    assert not verify_shopify_hmac(body, "", SECRET)
    assert not verify_shopify_hmac(body, headers["HTTP_X_SHOPIFY_HMAC_SHA256"], "")


def test_webhook_rejects_bad_signature(app):
    body, _ = _signed(ORDER)
    response = _call(app, "POST", "/webhooks/shopify/orders-paid", body, {"HTTP_X_SHOPIFY_HMAC_SHA256": "forged"})
    assert response["status"] == 401
    assert app.store.stats() == {}


def test_webhook_queues_one_job_per_platform_and_ignores_retries(app):
    body, headers = _signed(ORDER)
    assert _call(app, "POST", "/webhooks/shopify/orders-paid", body, headers)["status"] == 200
    assert _call(app, "POST", "/webhooks/shopify/orders-paid", body, headers)["status"] == 200  # Shopify retry
    stats = app.store.stats()
    assert set(stats) == {"meta", "tiktok", "snapchat", "linkedin", "google_ads"}  # not GA4
    assert all(v == {"pending": 1} for v in stats.values())


def test_collect_endpoint_cors_and_whitelist(app):
    beacon = {**BEACON, "order_id": "gid://shopify/OrderIdentity/9001", "checkout_token": "tok", "evil": "x"}
    beacon["cookies"] = {**BEACON["cookies"], "session": "secret"}
    response = _call(app, "POST", "/collect", json.dumps(beacon).encode(), {"HTTP_X_FORWARDED_FOR": "198.51.100.9, 10.0.0.1"})
    assert response["status"] == 204
    assert response["headers"]["Access-Control-Allow-Origin"] == "*"
    assert _call(app, "OPTIONS", "/collect")["status"] == 204
    assert _call(app, "POST", "/collect", b"not json")["status"] == 400
    assert _call(app, "POST", "/collect", b"x" * 9000)["status"] == 413

    body, headers = _signed(ORDER)
    _call(app, "POST", "/webhooks/shopify/orders-paid", body, headers)
    _, stored = app.store.order_and_beacon("9001")
    assert "session" not in stored["cookies"] and "evil" not in stored
    assert stored["ip"] == "198.51.100.9"


def test_clean_beacon_requires_explicit_true():
    assert clean_beacon({"marketing_allowed": "yes"})["marketing_allowed"] is False


# --- end to end: webhook + beacon -> worker -> fake platform APIs -----------


class FakePlatforms:
    """One local HTTP server standing in for every platform API."""

    def __init__(self):
        self.requests = []
        self.fail = {}  # path prefix -> status
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                raw = self.rfile.read(int(self.headers["Content-Length"]))
                outer.requests.append({"path": self.path, "headers": dict(self.headers), "raw": raw})
                for prefix, status in outer.fail.items():
                    if self.path.startswith(prefix):
                        self.send_response(status)
                        self.end_headers()
                        self.wfile.write(b'{"error": "boom"}')
                        return
                if self.path == "/token":
                    payload = {"access_token": "ya29.fake", "expires_in": 3600}
                elif self.path.startswith("/open_api"):
                    payload = {"code": 0, "message": "OK"}
                else:
                    payload = {"events_received": 1}
                data = json.dumps(payload).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def log_message(self, *args):
                pass

        self.server = HTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        base = f"http://127.0.0.1:{self.server.server_port}"
        self.endpoints = {p: base for p in ("meta", "tiktok", "snapchat", "linkedin", "google_ads")}
        self.endpoints["google_oauth"] = base + "/token"

    def bodies(self, prefix):
        return [json.loads(r["raw"]) for r in self.requests if r["path"].startswith(prefix)]


@pytest.fixture
def platforms():
    fake = FakePlatforms()
    yield fake
    fake.server.shutdown()


def _deliver(app, beacon=BEACON, token="tok"):
    if beacon is not None:
        _call(app, "POST", "/collect", json.dumps({**beacon, "order_id": 9001, "checkout_token": token}).encode())
    body, headers = _signed(ORDER)
    _call(app, "POST", "/webhooks/shopify/orders-paid", body, headers)


def _run_all(app, platforms, far_future=10**10):
    dispatcher = Dispatcher(app.config, SECRETS, platforms.endpoints)
    return process_due(app.store, dispatcher, app.config, now=far_future)


def test_end_to_end_sends_every_platform_once(app, platforms):
    _deliver(app)
    assert _run_all(app, platforms) == 5
    assert _run_all(app, platforms) == 0  # nothing sent twice
    assert {p: s for p, s in app.store.stats().items()} == {
        p: {"sent": 1} for p in ("meta", "tiktok", "snapchat", "linkedin", "google_ads")
    }
    meta = platforms.bodies("/v23.0/")[0]["data"][0]
    assert meta["event_id"] == "purchase-9001"
    assert meta["user_data"]["fbp"] == "fb.1.111.222"
    google = [r for r in platforms.requests if "uploadConversionAdjustments" in r["path"]][0]
    assert google["headers"]["Authorization"] == "Bearer ya29.fake"


def test_beacon_arriving_first_makes_jobs_due_now(app):
    _deliver(app)  # beacon posted before the webhook
    import time

    due = {row["platform"] for row in app.store.due_jobs(time.time() + 1)}
    assert due == {"meta", "tiktok", "snapchat", "linkedin"}  # Google Ads still waits


def test_consent_denied_sends_nothing(app, platforms):
    _deliver(app, beacon={"marketing_allowed": False})
    _run_all(app, platforms)
    assert platforms.requests == []
    job = app.store.job("9001", "meta")
    assert job["status"] == "skipped" and "declined" in job["detail"]


def test_no_consent_signal_is_skipped_by_default(app, platforms):
    _deliver(app, beacon=None)
    _run_all(app, platforms)
    assert platforms.requests == []
    assert "no consent signal" in app.store.job("9001", "tiktok")["detail"]


def test_no_consent_signal_can_be_allowed(app, platforms):
    app.config["capi"]["send_without_consent_signal"] = True
    _deliver(app, beacon=None)
    _run_all(app, platforms)
    assert app.store.job("9001", "meta")["status"] == "sent"
    meta = platforms.bodies("/v23.0/")[0]["data"][0]
    assert "fbp" not in meta["user_data"]  # no cookies without a beacon
    assert meta["user_data"]["fbc"].endswith(".FBCLID123")  # but the landing-page click ID still counts


def test_forged_beacon_for_other_checkout_is_ignored(app, platforms):
    _deliver(app, beacon={**BEACON, "cookies": {"_fbp": "attacker"}}, token="not-the-real-checkout")
    _run_all(app, platforms)
    # The mismatched beacon is dropped, so there is no consent signal either.
    assert platforms.requests == []
    assert "no consent signal" in app.store.job("9001", "meta")["detail"]


def test_retryable_failures_back_off_then_succeed(app, platforms):
    platforms.fail["/open_api"] = 503
    _deliver(app)
    _run_all(app, platforms)
    job = app.store.job("9001", "tiktok")
    assert job["status"] == "pending" and job["attempts"] == 1 and "503" in job["detail"]
    del platforms.fail["/open_api"]
    _run_all(app, platforms)
    assert app.store.job("9001", "tiktok")["status"] == "sent"


def test_client_errors_are_not_retried(app, platforms):
    platforms.fail["/rest/conversionEvents"] = 400
    _deliver(app)
    _run_all(app, platforms)
    job = app.store.job("9001", "linkedin")
    assert job["status"] == "failed" and "400" in job["detail"]


def test_tiktok_error_inside_200_is_a_failure():
    from tracking_agent.capi.platforms import _raise_for_body_errors

    with pytest.raises(SendError, match="40001"):
        _raise_for_body_errors({"code": 40001, "message": "invalid pixel"})
    _raise_for_body_errors({"code": 0, "message": "OK"})


def test_browser_and_server_event_ids_match(config):
    """The whole point: the pixel's purchase event_id equals the server's."""
    from tests.test_js_runtime import _events, _run_pixel  # noqa: PLC0415

    browser = _events(_run_pixel(config))["purchase"]
    server = from_shopify_order(ORDER, None)
    assert browser["event_id"] == server.event_id
    assert browser["ecommerce"]["transaction_id"] == server.order_id


# --- webhook registration + agent tools ------------------------------------


class FakeShopify:
    def __init__(self, existing=()):
        self.calls = []
        self.existing = list(existing)
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                outer.calls.append({"token": self.headers.get("X-Shopify-Access-Token"), **body})
                if "webhookSubscriptionCreate" in body["query"]:
                    data = {"webhookSubscriptionCreate": {
                        "webhookSubscription": {"id": "gid://shopify/WebhookSubscription/1", "topic": "ORDERS_PAID", "uri": body["variables"]["uri"]},
                        "userErrors": [],
                    }}
                else:
                    data = {"webhookSubscriptions": {"nodes": [{"id": "gid://shopify/WebhookSubscription/9", "uri": u} for u in outer.existing]}}
                payload = json.dumps({"data": data}).encode()
                self.send_response(200)
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

            def log_message(self, *args):
                pass

        self.server = HTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.url = f"http://127.0.0.1:{self.server.server_port}/graphql.json"


def test_shopify_admin_registers_webhook():
    from tracking_agent.capi.shopify_admin import ShopifyAdmin, webhook_uri

    fake = FakeShopify()
    try:
        admin = ShopifyAdmin("shop.myshopify.com", "shpat_x", "2026-07", endpoint=fake.url)
        uri = webhook_uri("https://capi.example.com/")
        assert uri == "https://capi.example.com/webhooks/shopify/orders-paid"
        assert admin.existing_webhook(uri) is None
        created = admin.create_webhook(uri)
    finally:
        fake.server.shutdown()
    assert created["uri"] == uri
    assert fake.calls[-1]["token"] == "shpat_x"
    assert fake.calls[-1]["variables"] == {"uri": uri}


def test_register_tool_needs_approval_and_skips_existing(tmp_path, capi_config, monkeypatch):
    import yaml

    from tracking_agent.capi import shopify_admin
    from tracking_agent.tools import Session, make_tools

    capi_config["shopify"]["myshopify_domain"] = "shop.myshopify.com"
    path = tmp_path / "tracking.yaml"
    path.write_text(yaml.safe_dump(capi_config))
    monkeypatch.setenv("SHOPIFY_ADMIN_TOKEN", "shpat_x")

    original = shopify_admin.ShopifyAdmin.__init__
    for existing, approve, expected in (
        (["https://capi.example.com/webhooks/shopify/orders-paid"], True, "already_registered"),
        ([], False, "declined"),
        ([], True, "created"),
    ):
        fake = FakeShopify(existing)
        monkeypatch.setattr(
            shopify_admin.ShopifyAdmin,
            "__init__",
            lambda self, d, t, v, endpoint=None, _u=fake.url, _o=original: _o(self, d, t, v, endpoint=_u),
        )
        session = Session(config_path=path, out_dir=tmp_path / "b", approve=lambda _s, a=approve: a)
        tools = {t.name: t for t in make_tools(session)}
        try:
            assert expected in tools["register_order_webhook"].call({})
        finally:
            fake.server.shutdown()
        created = [c for c in fake.calls if "webhookSubscriptionCreate" in c["query"]]
        assert bool(created) == (expected == "created")


def test_capi_status_never_reveals_secrets(tmp_path, capi_config, monkeypatch):
    import yaml

    from tracking_agent.tools import Session, make_tools

    monkeypatch.setenv("META_CAPI_ACCESS_TOKEN", "super-secret-token")
    path = tmp_path / "tracking.yaml"
    path.write_text(yaml.safe_dump(capi_config))
    session = Session(config_path=path, out_dir=tmp_path / "b", approve=lambda _s: False)
    output = {t.name: t for t in make_tools(session)}["capi_status"].call({})
    assert "super-secret-token" not in output
    status = json.loads(output)
    assert status["env_vars_present"]["META_CAPI_ACCESS_TOKEN"] is True
    assert "google_ads" in status["server_side_platforms"]


def test_checklist_includes_server_side_steps(capi_config):
    from tracking_agent.checklist import build_checklist

    text = build_checklist(capi_config)
    assert "Server-side conversions" in text
    assert "https://capi.example.com/webhooks/shopify/orders-paid" in text
    assert "protected customer data" in text
    assert "META_CAPI_ACCESS_TOKEN" in text
