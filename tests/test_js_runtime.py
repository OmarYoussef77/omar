"""Execute the generated JavaScript in Node with mocked Shopify / browser APIs.

1. The Shopify pixel receives realistic Shopify events and must push the
   right GA4-schema dataLayer events.
2. Every Custom HTML tag is rendered the way GTM would (variables replaced by
   values from that dataLayer push) and run, and the calls each platform's
   own snippet queues (fbq / ttq / snaptr / lintrk) are checked.
"""

import json
import re
import shutil
import subprocess

import pytest

from tracking_agent import gtm_builder, shopify_pixel

pytestmark = pytest.mark.skipif(not shutil.which("node"), reason="node not installed")

VARIANT = {
    "id": "4444",
    "sku": "TEE-RED-M",
    "title": "Red / M",
    "price": {"amount": 25.0, "currencyCode": "USD"},
    "product": {"id": "111", "title": "Classic Tee", "vendor": "Acme", "type": "Shirts"},
}
CONTEXT = {
    "document": {
        "location": {"href": "https://example-store.com/checkouts/c/abc/thank-you"},
        "referrer": "https://example-store.com/cart",
        "title": "Thank you",
    }
}
CHECKOUT = {
    "currencyCode": "USD",
    "totalPrice": {"amount": 55.0},
    "totalTax": {"amount": 5.0},
    "shippingLine": {"price": {"amount": 5.0}},
    "email": " Buyer@Example.com ",
    "phone": "+15555550123",
    "order": {"id": "9001"},
    "token": "tok",
    "discountApplications": [{"title": "WELCOME10"}],
    "lineItems": [{"quantity": 2, "title": "Classic Tee", "variant": VARIANT}],
}
SHOPIFY_EVENTS = [
    ("page_viewed", {"id": "evt-page", "context": CONTEXT, "data": {}}),
    ("product_viewed", {"id": "evt-view", "context": CONTEXT, "data": {"productVariant": VARIANT}}),
    (
        "product_added_to_cart",
        {
            "id": "evt-atc",
            "context": CONTEXT,
            "data": {
                "cartLine": {
                    "merchandise": VARIANT,
                    "quantity": 2,
                    "cost": {"totalAmount": {"amount": 50.0, "currencyCode": "USD"}},
                }
            },
        },
    ),
    ("search_submitted", {"id": "evt-search", "context": CONTEXT, "data": {"searchResult": {"query": "tee"}}}),
    ("checkout_completed", {"id": "evt-purchase", "context": CONTEXT, "data": {"checkout": CHECKOUT}}),
]

PIXEL_HARNESS = r"""
const vm = require('vm');
const input = JSON.parse(require('fs').readFileSync(0, 'utf8'));
const handlers = {};
const loaded = [];
const document = {
  createElement: () => ({}),
  getElementsByTagName: () => [{ parentNode: { insertBefore: (el) => loaded.push(el.src) } }],
};
const window = {};
const sandbox = {
  window, document, console,
  analytics: { subscribe: (name, fn) => { handlers[name] = fn; } },
  init: { customerPrivacy: { marketingAllowed: false, analyticsProcessingAllowed: true, preferencesProcessingAllowed: false } },
  customerPrivacy: { subscribe: (name, fn) => { handlers['privacy:' + name] = fn; } },
};
vm.runInNewContext(input.code, sandbox);
for (const [name, event] of input.events) handlers[name](event);
handlers['privacy:visitorConsentCollected']({ customerPrivacy: { marketingAllowed: true, analyticsProcessingAllowed: true, preferencesProcessingAllowed: true } });
const dataLayer = window.dataLayer.map((e) => (typeof e === 'object' && e.length !== undefined && !Array.isArray(e) ? Array.from(e) : e));
console.log(JSON.stringify({ dataLayer, loaded, subscribed: Object.keys(handlers) }));
"""

TAG_HARNESS = r"""
const vm = require('vm');
const input = JSON.parse(require('fs').readFileSync(0, 'utf8'));
const loaded = [];
const el = () => ({});
const document = {
  createElement: el,
  getElementsByTagName: () => [{ parentNode: { insertBefore: (n) => loaded.push(n.src) } }],
};
const window = { document };
window.window = window;
const ctx = vm.createContext(window);
for (const script of input.scripts) vm.runInContext(script, ctx);
const calls = {
  fbq: window.fbq ? window.fbq.queue.map((a) => Array.from(a)) : null,
  ttq: window.ttq ? Array.from(window.ttq) : null,
  snaptr: window.snaptr ? window.snaptr.queue.map((a) => Array.from(a)) : null,
  lintrk: window.lintrk ? window.lintrk.q : null,
};
console.log(JSON.stringify({ calls, loaded }));
"""


def _node(harness: str, payload: dict) -> dict:
    result = subprocess.run(
        ["node", "-e", harness], input=json.dumps(payload), capture_output=True, text=True, timeout=30
    )
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


def _run_pixel(config):
    code = shopify_pixel.build_pixel(config)
    return _node(PIXEL_HARNESS, {"code": code, "events": SHOPIFY_EVENTS})


def _events(output):
    return {e["event"]: e for e in output["dataLayer"] if isinstance(e, dict) and "event" in e}


def test_pixel_loads_gtm_and_subscribes(config):
    out = _run_pixel(config)
    assert out["loaded"] == ["https://www.googletagmanager.com/gtm.js?id=GTM-ABC1234"]
    for name in ("page_viewed", "checkout_completed", "payment_info_submitted", "collection_viewed"):
        assert name in out["subscribed"]


def test_pixel_consent_mode(config):
    commands = [e for e in _run_pixel(config)["dataLayer"] if isinstance(e, list)]
    default = next(c for c in commands if c[:2] == ["consent", "default"])
    assert default[2]["ad_storage"] == "denied"
    assert default[2]["analytics_storage"] == "granted"
    update = next(c for c in commands if c[:2] == ["consent", "update"])
    assert update[2]["ad_storage"] == "granted"


def test_pixel_purchase_event(config):
    events = _events(_run_pixel(config))
    purchase = events["purchase"]
    assert purchase["event_id"] == "evt-purchase"
    assert purchase["page_location"].endswith("/thank-you")
    assert purchase["ecommerce"]["transaction_id"] == "9001"
    assert purchase["ecommerce"]["value"] == 55.0
    assert purchase["ecommerce"]["coupon"] == "WELCOME10"
    assert purchase["ecommerce"]["items"][0]["item_id"] == "shopify_US_111_4444"
    assert purchase["ecommerce"]["items"][0]["quantity"] == 2
    assert purchase["content_ids"] == ["shopify_US_111_4444"]
    assert purchase["num_items"] == 2
    assert purchase["user_data"]["email"] == "buyer@example.com"
    assert events["search"]["search_term"] == "tee"
    assert events["add_to_cart"]["ecommerce"]["value"] == 50.0


@pytest.mark.parametrize("source,expected", [("variant_id", "4444"), ("product_id", "111"), ("sku", "TEE-RED-M")])
def test_pixel_item_id_source(config, source, expected):
    config["store"]["item_id_source"] = source
    assert _events(_run_pixel(config))["view_item"]["ecommerce"]["items"][0]["item_id"] == expected


def test_pixel_clears_ecommerce_between_events(config):
    layer = _run_pixel(config)["dataLayer"]
    i = next(i for i, e in enumerate(layer) if isinstance(e, dict) and e.get("event") == "purchase")
    assert layer[i - 1] == {"ecommerce": None}


# --- GTM Custom HTML tags --------------------------------------------------


def _get_path(data, dotted):
    for key in dotted.split("."):
        if not isinstance(data, dict) or key not in data:
            return None
        data = data[key]
    return data


def _render(html: str, export: dict, datalayer_event: dict) -> str:
    """Substitute GTM variables the way GTM does in Custom HTML (as JS values)."""
    cv = export["containerVersion"]
    values = {}
    for var in cv["variable"]:
        params = {p["key"]: p["value"] for p in var["parameter"]}
        values[var["name"]] = params["value"] if var["type"] == "c" else _get_path(datalayer_event, params["name"])
    script = re.sub(r"</?script>", "", html)
    return re.sub(r"\{\{(.+?)\}\}", lambda m: json.dumps(values[m.group(1)]), script)


def _fire(config, tag_names, event_name):
    export = gtm_builder.build_container(config)
    tags = {t["name"]: t for t in export["containerVersion"]["tag"]}
    event = _events(_run_pixel(config))[event_name]
    scripts = []
    for name in tag_names:
        html = next(p["value"] for p in tags[name]["parameter"] if p["key"] == "html")
        scripts.append(_render(html, export, event))
    return _node(TAG_HARNESS, {"scripts": scripts})


def test_all_custom_html_tags_execute(config):
    export = gtm_builder.build_container(config)
    tags = export["containerVersion"]["tag"]
    events = _events(_run_pixel(config))
    triggers = {t["triggerId"]: t["name"].removeprefix("CE - ") for t in export["containerVersion"]["trigger"]}
    by_name = {t["name"]: t for t in tags}
    for tag in tags:
        if tag["type"] != "html":
            continue
        event = events.get(triggers[tag["firingTriggerId"][0]], events["purchase"])
        chain = [s["tagName"] for s in tag.get("setupTag", [])] + [tag["name"]]
        scripts = [
            _render(next(p["value"] for p in by_name[n]["parameter"] if p["key"] == "html"), export, event)
            for n in chain
        ]
        _node(TAG_HARNESS, {"scripts": scripts})  # asserts node exits cleanly


def test_meta_purchase_tag(config):
    out = _fire(config, ["Meta - Base (init)", "Meta - Purchase"], "purchase")
    init, track = out["calls"]["fbq"]
    assert init == ["init", "1234567890123456", {"em": "buyer@example.com", "ph": "+15555550123"}]
    assert track[:2] == ["track", "Purchase"]
    assert track[2]["value"] == 55.0 and track[2]["currency"] == "USD"
    assert track[2]["content_ids"] == ["shopify_US_111_4444"]
    assert track[2]["contents"] == [{"id": "shopify_US_111_4444", "quantity": 2, "item_price": 25.0}]
    assert track[3] == {"eventID": "evt-purchase"}
    assert "https://connect.facebook.net/en_US/fbevents.js" in out["loaded"]


def test_tiktok_purchase_tag(config):
    out = _fire(config, ["TikTok - Base (init)", "TikTok - CompletePayment"], "purchase")
    calls = out["calls"]["ttq"]
    assert calls[0][0] == "identify"
    method, name, payload, options = calls[-1]
    assert (method, name) == ("track", "CompletePayment")
    assert payload["value"] == 55.0
    assert payload["contents"][0]["content_id"] == "shopify_US_111_4444"
    assert options == {"event_id": "evt-purchase"}
    assert any("analytics.tiktok.com" in src for src in out["loaded"])


def test_snap_purchase_tag(config):
    out = _fire(config, ["Snap - Base (init)", "Snap - PURCHASE"], "purchase")
    init, track = out["calls"]["snaptr"]
    assert init[:2] == ["init", "0f1e2d3c-4b5a-6978-8a9b-0c1d2e3f4a5b"]
    assert init[2]["user_email"] == "buyer@example.com"
    assert track[:2] == ["track", "PURCHASE"]
    assert track[2]["transaction_id"] == "9001"
    assert track[2]["client_dedup_id"] == "evt-purchase"
    assert track[2]["item_ids"] == ["shopify_US_111_4444"]


def test_linkedin_conversion_tag(config):
    out = _fire(config, ["LinkedIn - Insight Tag", "LinkedIn - Conversion - purchase"], "purchase")
    assert out["calls"]["lintrk"] == [["track", {"conversion_id": 12345678, "event_id": "evt-purchase"}]]
    assert "https://snap.licdn.com/li.lms-analytics/insight.min.js" in out["loaded"]


def test_search_tags(config):
    out = _fire(config, ["Meta - Base (init)", "Meta - Search"], "search")
    assert out["calls"]["fbq"][-1][2] == {"search_string": "tee"}
