"""Run the real browser driver against a local mock Shopify store.

The mock pages emit tracking requests in each platform's wire format (the
same shapes their browser libraries send). Those requests are answered
locally by Playwright routes, so nothing leaves the machine. This tests the
driver, the capture and the report together in real Chromium; the platform
snippets themselves are covered by test_js_runtime.py.
"""

import copy
import json
import os
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from tracking_agent.checkout_test.capture import classify
from tracking_agent.checkout_test.report import evaluate
from tracking_agent.checkout_test.runner import run_checkout_test

playwright = pytest.importorskip("playwright.sync_api")

TRACKING_HOSTS = (
    "www.facebook.com",
    "analytics.tiktok.com",
    "tr.snapchat.com",
    "px.ads.linkedin.com",
    "region1.google-analytics.com",
    "www.googleadservices.com",
    "capi.example.com",
)

EMIT_JS = r"""
<script>
const CFG = __CFG__;
const MODE = __MODE__;
function send(url, opts) { fetch(url, Object.assign({mode: 'no-cors'}, opts || {})).catch(() => {}); }
function emit(step, eventId) {
  const ids = JSON.stringify(['shopify_US_111_4444']);
  const meta = {page_view: 'PageView', view_item: 'ViewContent', add_to_cart: 'AddToCart',
    begin_checkout: 'InitiateCheckout', add_payment_info: 'AddPaymentInfo', purchase: 'Purchase'}[step];
  send('https://www.facebook.com/tr/?id=' + CFG.meta + '&ev=' + meta + '&eid=' + eventId +
       '&cd[value]=55&cd[currency]=USD&cd[content_ids]=' + encodeURIComponent(ids));
  if (step === 'purchase' && MODE === 'broken') {
    // A second, leftover Meta pixel also reporting the purchase.
    send('https://www.facebook.com/tr/?id=999999999999999&ev=Purchase&eid=legacy-1&cd[value]=55&cd[currency]=USD');
  }
  const tt = {page_view: 'Pageview', view_item: 'ViewContent', add_to_cart: 'AddToCart',
    begin_checkout: 'InitiateCheckout', add_payment_info: 'AddPaymentInfo', purchase: 'CompletePayment'}[step];
  if (!(step === 'purchase' && MODE === 'broken')) {
    send('https://analytics.tiktok.com/api/v2/pixel', {method: 'POST', body: JSON.stringify({
      event: tt, event_id: eventId, context: {pixel: {code: CFG.tiktok}},
      properties: {value: 55, currency: 'USD', contents: [{content_id: 'shopify_US_111_4444'}]}})});
  }
  const snap = {page_view: 'PAGE_VIEW', view_item: 'VIEW_CONTENT', add_to_cart: 'ADD_CART',
    begin_checkout: 'START_CHECKOUT', add_payment_info: 'ADD_BILLING', purchase: 'PURCHASE'}[step];
  send('https://tr.snapchat.com/p', {method: 'POST', headers: {'Content-Type': 'application/x-www-form-urlencoded'},
    body: 'pid=' + CFG.snap + '&ev=' + snap + '&client_dedup_id=' + eventId});
  if (step === 'page_view') send('https://px.ads.linkedin.com/collect/?pid=' + CFG.linkedin + '&fmt=js');
  if (step === 'purchase') send('https://px.ads.linkedin.com/collect/?pid=' + CFG.linkedin + '&conversionId=12345678&fmt=js');
  send('https://region1.google-analytics.com/g/collect?v=2&tid=' + CFG.ga4 + '&cu=USD',
    {method: 'POST', body: 'en=' + step + (step === 'purchase' ? '&ep.transaction_id=9001&epn.value=55' : '')});
  const ads = {add_to_cart: 'QrStUvWxYz123456', purchase: 'AbCdEfGhIjKlMnOp'}[step];
  if (ads) send('https://www.googleadservices.com/pagead/conversion/123456789/?label=' + ads + '&value=55&currency_code=USD&oid=9001');
  if (step === 'purchase') send('https://capi.example.com/collect', {method: 'POST', body: JSON.stringify({order_id: '9001', checkout_token: 'tok'})});
}
</script>
"""

PAGES = {
    "/": "<h1>Home</h1><script>emit('page_view', 'e1')</script>",
    "/products/tee": """
      <h1>Classic Tee</h1>
      <script>emit('page_view', 'e2'); emit('view_item', 'e3');</script>
      <form action="/cart/add" method="post" onsubmit="event.preventDefault();
        fetch('/cart/add.js', {method: 'POST'}).then(() => emit('add_to_cart', 'e4'));">
        <button type="submit" name="add">Add to cart</button>
      </form>""",
    "/checkout": """
      <script>emit('begin_checkout', 'e5');</script>
      <input name="email"> <input name="firstName"> <input name="lastName"> <input name="address1">
      <input name="city"> <select name="zone"><option value="CA">CA</option><option value="NY">NY</option></select>
      <input name="postalCode">
      <iframe name="card-fields-number-1" srcdoc="<input name='number'>"></iframe>
      <iframe name="card-fields-expiry-1" srcdoc="<input name='expiry'>"></iframe>
      <iframe name="card-fields-verification_value-1" srcdoc="<input name='verification_value'>"></iframe>
      <iframe name="card-fields-name-1" srcdoc="<input name='name'>"></iframe>
      <button id="checkout-pay-button" onclick="
        const card = document.querySelector('iframe[name^=card-fields-number]').contentDocument.querySelector('input').value;
        if (card !== '1') { document.body.insertAdjacentHTML('beforeend', '<p>declined</p>'); return; }
        emit('add_payment_info', 'e6');
        setTimeout(() => { location.href = '/checkouts/c/abc/thank-you'; }, 200);">Pay now</button>""",
    "/checkouts/c/abc/thank-you": "<h1>Thank you</h1><script>emit('purchase', 'purchase-9001')</script>",
}


class MockStore:
    def __init__(self, config, mode="ok"):
        ids = {
            "meta": config["platforms"]["meta"]["pixel_id"],
            "tiktok": config["platforms"]["tiktok"]["pixel_id"],
            "snap": config["platforms"]["snapchat"]["pixel_id"],
            "linkedin": config["platforms"]["linkedin"]["partner_id"],
            "ga4": config["platforms"]["ga4"]["measurement_id"],
        }
        head = EMIT_JS.replace("__CFG__", json.dumps(ids)).replace("__MODE__", json.dumps(mode))

        class Handler(BaseHTTPRequestHandler):
            def _send(self, status, body, content_type="text/html"):
                data = body.encode()
                self.send_response(status)
                self.send_header("Content-Type", content_type)
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def do_GET(self):
                path = self.path.split("?")[0]
                if path == "/products.json":
                    return self._send(200, json.dumps({"products": [{"handle": "tee"}]}), "application/json")
                if path in PAGES:
                    return self._send(200, f"<html><head>{head}</head><body>{PAGES[path]}</body></html>")
                self._send(404, "not found")

            def do_POST(self):
                self.rfile.read(int(self.headers.get("Content-Length") or 0))
                self._send(200, "{}", "application/json")

            def log_message(self, *args):
                pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.url = f"http://127.0.0.1:{self.server.server_port}"


def _offline_tracking(context):
    for host in TRACKING_HOSTS:
        context.route(f"https://{host}/**", lambda route: route.fulfill(status=204, body=""))


@pytest.fixture
def test_config(config):
    config = copy.deepcopy(config)
    config["capi"]["enabled"] = True
    config["capi"]["server_url"] = "https://capi.example.com"
    return config


def _run(config, mode="ok", purchase=True):
    store = MockStore(config, mode)
    config["checkout_test"]["store_url"] = store.url
    try:
        return run_checkout_test(config, purchase=purchase, settle_ms=500, configure_context=_offline_tracking)
    finally:
        store.server.shutdown()


def _chromium_available():
    return os.path.exists("/opt/pw-browsers/chromium") or os.environ.get("TRACKING_AGENT_CHROMIUM")


pytestmark = pytest.mark.skipif(not _chromium_available(), reason="no Chromium available")


def test_full_purchase_passes(test_config):
    report = _run(test_config)
    assert report["errors"] == []
    assert report["problems"] == [], report["problems"]
    assert report["status"] == "pass", report["warnings"]
    assert report["order_id"] == "9001"
    assert report["final_url"].endswith("/thank-you")
    assert report["steps"]["purchase"]["meta"] == {"expected": "Purchase", "seen": 1}
    assert report["steps"]["purchase"]["google_ads"] == {"expected": "conversion:AbCdEfGhIjKlMnOp", "seen": 1}
    assert report["steps"]["add_payment_info"]["tiktok"]["seen"] == 1


def test_browse_mode_never_pays(test_config):
    report = _run(test_config, purchase=False)
    assert report["mode"] == "browse"
    assert report["final_url"].endswith("/checkout")
    assert "purchase" not in report["steps"]
    assert report["status"] == "pass", report["problems"] + report["warnings"]


def test_broken_setup_is_reported(test_config):
    report = _run(test_config, mode="broken")
    assert report["status"] == "fail"
    problems = " ".join(report["problems"])
    assert "tiktok 'CompletePayment' did not fire" in problems
    warnings = " ".join(report["warnings"])
    assert "unexpected meta pixel 999999999999999" in warnings


def test_driver_errors_are_reported(test_config):
    test_config["checkout_test"]["selectors"] = {"add_to_cart": "#does-not-exist"}
    test_config["checkout_test"]["store_url"] = "unused"
    store = MockStore(test_config)
    test_config["checkout_test"]["store_url"] = store.url
    try:
        report = run_checkout_test(test_config, settle_ms=200, configure_context=_offline_tracking)
    finally:
        store.server.shutdown()
    assert report["status"] == "fail"
    assert report["errors"] and report["errors"][0].startswith("add_to_cart:")
    assert any("begin_checkout: the test never reached" in p for p in report["problems"])


# --- parsers on their own ----------------------------------------------------


def test_classify_formats():
    (meta,) = classify("https://www.facebook.com/tr/?id=1&ev=Purchase&eid=purchase-9&cd[value]=5&cd[currency]=USD&cd[content_ids]=%5B%22a%22%5D")
    assert (meta.platform, meta.event, meta.event_id, meta.value, meta.content_ids) == ("meta", "Purchase", "purchase-9", 5.0, ["a"])
    (meta_post,) = classify("https://www.facebook.com/tr/", "POST", "id=1&ev=AddToCart&eid=x")
    assert meta_post.event == "AddToCart"
    batch = classify("https://analytics.tiktok.com/api/v2/pixel/batch", "POST", json.dumps({"batch": [
        {"event": "ViewContent", "context": {"pixel": {"code": "C1"}}}, {"event": "AddToCart"}]}))
    assert [h.event for h in batch] == ["ViewContent", "AddToCart"] and batch[0].pixel_id == "C1"
    ga = classify("https://region1.google-analytics.com/g/collect?v=2&tid=G-1&cu=EUR", "POST", "en=view_item\nen=add_to_cart&epn.value=3")
    assert [h.event for h in ga] == ["view_item", "add_to_cart"] and ga[1].value == 3.0 and ga[1].currency == "EUR"
    (li,) = classify("https://px.ads.linkedin.com/collect/?pid=77&conversionId=88&fmt=js")
    assert li.event == "conversion:88"
    (ads,) = classify("https://www.google.com/pagead/1p-conversion/123/?label=L&value=1&oid=9")
    assert (ads.event, ads.pixel_id, ads.event_id) == ("conversion:L", "AW-123", "9")
    (remarketing,) = classify("https://googleads.g.doubleclick.net/pagead/viewthroughconversion/123/?random=1")
    assert remarketing.event == "remarketing"
    assert classify("https://example.com/tr?ev=Purchase") == []
    (beacon,) = classify("https://capi.example.com/collect", "POST", '{"order_id": "5"}', "https://capi.example.com/collect")
    assert beacon.platform == "capi_beacon" and beacon.event_id == "purchase-5"


def test_duplicate_purchase_detected(test_config):
    hits = []
    for eid in ("purchase-9001", "legacy-77"):
        (hit,) = classify(f"https://www.facebook.com/tr/?id=1234567890123456&ev=Purchase&eid={eid}&cd[value]=1&cd[currency]=USD&cd[content_ids]=%5B%22a%22%5D")
        hit.step = "purchase"
        hits.append(hit)
    report = evaluate(hits, test_config, purchase=True, reached=["purchase"])
    assert any("fired 2 times" in p and "legacy-77" in p for p in report["problems"])
    assert any("event ID 'legacy-77'" in w for w in report["warnings"])
