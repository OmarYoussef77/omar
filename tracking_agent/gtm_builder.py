"""Build an importable Google Tag Manager container (export format v2).

The container listens for the GA4-style dataLayer events that the Shopify
custom pixel pushes (see shopify_pixel.py) and fans them out to each enabled
platform. Generation is deterministic: the model decides *what* to build,
this module decides *how*, so the output is always structurally valid.

Import it in GTM via Admin > Import Container > (new workspace) > Merge.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from .config import enabled_platforms

# Platform event names per canonical (GA4) event. None = not sent.
META_EVENTS = {
    "page_view": "PageView",
    "view_item": "ViewContent",
    "search": "Search",
    "add_to_cart": "AddToCart",
    "begin_checkout": "InitiateCheckout",
    "add_payment_info": "AddPaymentInfo",
    "purchase": "Purchase",
}
TIKTOK_EVENTS = {
    "view_item": "ViewContent",
    "search": "Search",
    "add_to_cart": "AddToCart",
    "begin_checkout": "InitiateCheckout",
    "add_payment_info": "AddPaymentInfo",
    "purchase": "CompletePayment",
}
SNAP_EVENTS = {
    "page_view": "PAGE_VIEW",
    "view_item_list": "LIST_VIEW",
    "view_item": "VIEW_CONTENT",
    "search": "SEARCH",
    "add_to_cart": "ADD_CART",
    "begin_checkout": "START_CHECKOUT",
    "add_payment_info": "ADD_BILLING",
    "purchase": "PURCHASE",
}
GA4_EVENTS = (
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

# dataLayer keys pushed by the Shopify pixel -> GTM variable name.
DATA_LAYER_VARIABLES = {
    "event_id": "DLV - event_id",
    "page_location": "DLV - page_location",
    "page_referrer": "DLV - page_referrer",
    "page_title": "DLV - page_title",
    "search_term": "DLV - search_term",
    "content_ids": "DLV - content_ids",
    "num_items": "DLV - num_items",
    "user_data.email": "DLV - user_data.email",
    "user_data.phone": "DLV - user_data.phone",
    "ecommerce.value": "DLV - ecommerce.value",
    "ecommerce.currency": "DLV - ecommerce.currency",
    "ecommerce.transaction_id": "DLV - ecommerce.transaction_id",
    "ecommerce.items": "DLV - ecommerce.items",
}

AD_CONSENT = {
    "consentStatus": "NEEDED",
    "consentType": {"type": "LIST", "list": [{"type": "TEMPLATE", "value": "ad_storage"}]},
}


def v(name: str) -> str:
    """GTM variable reference."""
    return "{{" + name + "}}"


DLV = {key: v(name) for key, name in DATA_LAYER_VARIABLES.items()}


def _template(key: str, value: str) -> dict[str, str]:
    return {"type": "TEMPLATE", "key": key, "value": value}


def _boolean(key: str, value: bool) -> dict[str, str]:
    return {"type": "BOOLEAN", "key": key, "value": "true" if value else "false"}


@dataclass
class ContainerBuilder:
    public_id: str
    name: str = "Shopify tracking"
    variables: list[dict[str, Any]] = field(default_factory=list)
    triggers: list[dict[str, Any]] = field(default_factory=list)
    tags: list[dict[str, Any]] = field(default_factory=list)
    _next_id: int = 1
    _trigger_ids: dict[str, str] = field(default_factory=dict)

    def _id(self) -> str:
        value = str(self._next_id)
        self._next_id += 1
        return value

    def _base(self) -> dict[str, str]:
        return {"accountId": "0", "containerId": "0"}

    def data_layer_variable(self, name: str, key: str) -> None:
        self.variables.append(
            {
                **self._base(),
                "variableId": self._id(),
                "name": name,
                "type": "v",
                "parameter": [
                    {"type": "INTEGER", "key": "dataLayerVersion", "value": "2"},
                    _boolean("setDefaultValue", False),
                    _template("name", key),
                ],
            }
        )

    def constant(self, name: str, value: str) -> str:
        self.variables.append(
            {
                **self._base(),
                "variableId": self._id(),
                "name": name,
                "type": "c",
                "parameter": [_template("value", value)],
            }
        )
        return v(name)

    def event_trigger(self, event: str) -> str:
        """Custom Event trigger for a dataLayer event (created once)."""
        if event not in self._trigger_ids:
            trigger_id = self._id()
            self.triggers.append(
                {
                    **self._base(),
                    "triggerId": trigger_id,
                    "name": f"CE - {event}",
                    "type": "CUSTOM_EVENT",
                    "customEventFilter": [
                        {
                            "type": "EQUALS",
                            "parameter": [_template("arg0", "{{_event}}"), _template("arg1", event)],
                        }
                    ],
                }
            )
            self._trigger_ids[event] = trigger_id
        return self._trigger_ids[event]

    def tag(
        self,
        name: str,
        tag_type: str,
        parameters: list[dict[str, Any]],
        event: str,
        *,
        firing: str = "ONCE_PER_EVENT",
        setup_tag: str | None = None,
        ad_consent: bool = False,
    ) -> None:
        tag: dict[str, Any] = {
            **self._base(),
            "tagId": self._id(),
            "name": name,
            "type": tag_type,
            "parameter": parameters,
            "firingTriggerId": [self.event_trigger(event)],
            "tagFiringOption": firing,
        }
        if setup_tag:
            tag["setupTag"] = [{"tagName": setup_tag, "stopOnSetupFailure": True}]
        if ad_consent:
            tag["consentSettings"] = AD_CONSENT
        self.tags.append(tag)

    def html_tag(self, name: str, html: str, event: str, **kwargs: Any) -> None:
        self.tag(
            name,
            "html",
            [_template("html", html.strip() + "\n"), _boolean("supportDocumentWrite", False)],
            event,
            **kwargs,
        )

    def export(self) -> dict[str, Any]:
        return {
            "exportFormatVersion": 2,
            "exportTime": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S"),
            "containerVersion": {
                "path": "accounts/0/containers/0/versions/0",
                **self._base(),
                "containerVersionId": "0",
                "container": {
                    "path": "accounts/0/containers/0",
                    **self._base(),
                    "name": self.name,
                    "publicId": self.public_id,
                    "usageContext": ["WEB"],
                },
                "tag": self.tags,
                "trigger": self.triggers,
                "variable": self.variables,
            },
        }


# ---------------------------------------------------------------------------
# Platform tag sets. Custom HTML must be ES5: GTM rejects arrow functions,
# let/const and template literals in Custom HTML tags.
# ---------------------------------------------------------------------------

_ITEMS = "(" + DLV["ecommerce.items"] + " || [])"


def _add_ga4(b: ContainerBuilder, cfg: dict[str, Any]) -> None:
    measurement_id = b.constant("CONST - GA4 Measurement ID", cfg["measurement_id"])
    # The pixel runs in Shopify's sandboxed iframe, so the page URL/title
    # must come from the dataLayer instead of the iframe's own location.
    page_fields = {
        "page_location": DLV["page_location"],
        "page_referrer": DLV["page_referrer"],
        "page_title": DLV["page_title"],
    }
    b.tag(
        "GA4 - Google tag",
        "googtag",
        [
            _template("tagId", measurement_id),
            {
                "type": "LIST",
                "key": "configSettingsTable",
                "list": [
                    {
                        "type": "MAP",
                        "map": [_template("parameter", k), _template("parameterValue", val)],
                    }
                    for k, val in page_fields.items()
                ],
            },
        ],
        "page_view",
    )
    for event in GA4_EVENTS:
        params = [
            _template("eventName", event),
            _template("measurementIdOverride", measurement_id),
            _boolean("sendEcommerceData", True),
            _template("getEcommerceDataFrom", "dataLayer"),
        ]
        if event == "search":
            params.append(
                {
                    "type": "LIST",
                    "key": "eventSettingsTable",
                    "list": [
                        {
                            "type": "MAP",
                            "map": [
                                _template("parameter", "search_term"),
                                _template("parameterValue", DLV["search_term"]),
                            ],
                        }
                    ],
                }
            )
        b.tag(f"GA4 - {event}", "gaawe", params, event)


def _add_google_ads(b: ContainerBuilder, cfg: dict[str, Any]) -> None:
    aw_id = cfg["conversion_id"]
    b.tag("Google Ads - Conversion Linker", "gclidw", [], "page_view")
    b.tag("Google Ads - Google tag", "googtag", [_template("tagId", aw_id)], "page_view")
    numeric_id = aw_id.removeprefix("AW-")
    for event, label in (cfg.get("conversions") or {}).items():
        params = [
            _template("conversionId", numeric_id),
            _template("conversionLabel", str(label)),
        ]
        if event != "page_view":
            params += [
                _template("conversionValue", DLV["ecommerce.value"]),
                _template("currencyCode", DLV["ecommerce.currency"]),
            ]
        if event == "purchase":
            params.append(_template("orderId", DLV["ecommerce.transaction_id"]))
        b.tag(f"Google Ads - Conversion - {event}", "awct", params, event)


def _add_meta(b: ContainerBuilder, cfg: dict[str, Any]) -> None:
    pixel_id = b.constant("CONST - Meta Pixel ID", cfg["pixel_id"])
    base = "Meta - Base (init)"
    b.html_tag(
        base,
        f"""
<script>
!function(f,b,e,v,n,t,s){{if(f.fbq)return;n=f.fbq=function(){{n.callMethod?
n.callMethod.apply(n,arguments):n.queue.push(arguments)}};if(!f._fbq)f._fbq=n;
n.push=n;n.loaded=!0;n.version='2.0';n.queue=[];t=b.createElement(e);t.async=!0;
t.src=v;s=b.getElementsByTagName(e)[0];s.parentNode.insertBefore(t,s)}}(window,
document,'script','https://connect.facebook.net/en_US/fbevents.js');
(function() {{
  var am = {{}};
  if ({DLV['user_data.email']}) am.em = {DLV['user_data.email']};
  if ({DLV['user_data.phone']}) am.ph = {DLV['user_data.phone']};
  fbq('init', {pixel_id}, am);
}})();
</script>
""",
        "page_view",
        firing="ONCE_PER_LOAD",
        ad_consent=True,
    )
    for event, meta_event in META_EVENTS.items():
        if event == "page_view":
            payload = "{}"
        elif event == "search":
            payload = f"{{search_string: {DLV['search_term']}}}"
        else:
            payload = f"""{{
    value: {DLV['ecommerce.value']},
    currency: {DLV['ecommerce.currency']},
    content_ids: {DLV['content_ids']},
    content_type: 'product',
    contents: {_ITEMS}.map(function(i) {{ return {{id: i.item_id, quantity: i.quantity, item_price: i.price}}; }}),
    num_items: {DLV['num_items']}
  }}"""
        b.html_tag(
            f"Meta - {meta_event}",
            f"<script>\n  fbq('track', '{meta_event}', {payload}, {{eventID: {DLV['event_id']}}});\n</script>",
            event,
            setup_tag=base,
            ad_consent=True,
        )


def _add_tiktok(b: ContainerBuilder, cfg: dict[str, Any]) -> None:
    pixel_id = b.constant("CONST - TikTok Pixel ID", cfg["pixel_id"])
    base = "TikTok - Base (init)"
    b.html_tag(
        base,
        f"""
<script>
!function (w, d, t) {{
  w.TiktokAnalyticsObject=t;var ttq=w[t]=w[t]||[];ttq.methods=["page","track","identify","instances","debug","on","off","once","ready","alias","group","enableCookie","disableCookie","holdConsent","revokeConsent","grantConsent"],ttq.setAndDefer=function(t,e){{t[e]=function(){{t.push([e].concat(Array.prototype.slice.call(arguments,0)))}}}};for(var i=0;i<ttq.methods.length;i++)ttq.setAndDefer(ttq,ttq.methods[i]);ttq.instance=function(t){{for(var e=ttq._i[t]||[],n=0;n<ttq.methods.length;n++)ttq.setAndDefer(e,ttq.methods[n]);return e}},ttq.load=function(e,n){{var r="https://analytics.tiktok.com/i18n/pixel/events.js",o=n&&n.partner;ttq._i=ttq._i||{{}},ttq._i[e]=[],ttq._i[e]._u=r,ttq._t=ttq._t||{{}},ttq._t[e]=+new Date,ttq._o=ttq._o||{{}},ttq._o[e]=n||{{}};n=document.createElement("script");n.type="text/javascript",n.async=!0,n.src=r+"?sdkid="+e+"&lib="+t;e=document.getElementsByTagName("script")[0];e.parentNode.insertBefore(n,e)}};
  ttq.load({pixel_id});
}}(window, document, 'ttq');
(function() {{
  var id = {{}};
  if ({DLV['user_data.email']}) id.email = {DLV['user_data.email']};
  if ({DLV['user_data.phone']}) id.phone_number = {DLV['user_data.phone']};
  if (id.email || id.phone_number) ttq.identify(id);
}})();
</script>
""",
        "page_view",
        firing="ONCE_PER_LOAD",
        ad_consent=True,
    )
    b.html_tag("TikTok - Page", "<script>\n  ttq.page();\n</script>", "page_view", setup_tag=base, ad_consent=True)
    for event, tt_event in TIKTOK_EVENTS.items():
        if event == "search":
            payload = f"{{query: {DLV['search_term']}}}"
        else:
            payload = f"""{{
    value: {DLV['ecommerce.value']},
    currency: {DLV['ecommerce.currency']},
    contents: {_ITEMS}.map(function(i) {{
      return {{content_id: i.item_id, content_type: 'product', content_name: i.item_name, quantity: i.quantity, price: i.price}};
    }})
  }}"""
        b.html_tag(
            f"TikTok - {tt_event}",
            f"<script>\n  ttq.track('{tt_event}', {payload}, {{event_id: {DLV['event_id']}}});\n</script>",
            event,
            setup_tag=base,
            ad_consent=True,
        )


def _add_snapchat(b: ContainerBuilder, cfg: dict[str, Any]) -> None:
    pixel_id = b.constant("CONST - Snap Pixel ID", cfg["pixel_id"])
    base = "Snap - Base (init)"
    b.html_tag(
        base,
        f"""
<script>
(function(e,t,n){{if(e.snaptr)return;var a=e.snaptr=function()
{{a.handleRequest?a.handleRequest.apply(a,arguments):a.queue.push(arguments)}};
a.queue=[];var s='script';var r=t.createElement(s);r.async=!0;
r.src=n;var u=t.getElementsByTagName(s)[0];
u.parentNode.insertBefore(r,u);}})(window,document,'https://sc-static.net/scevent.min.js');
(function() {{
  var am = {{}};
  if ({DLV['user_data.email']}) am.user_email = {DLV['user_data.email']};
  if ({DLV['user_data.phone']}) am.user_phone_number = {DLV['user_data.phone']};
  snaptr('init', {pixel_id}, am);
}})();
</script>
""",
        "page_view",
        firing="ONCE_PER_LOAD",
        ad_consent=True,
    )
    for event, snap_event in SNAP_EVENTS.items():
        fields = [f"client_dedup_id: {DLV['event_id']}"]
        if event == "search":
            fields.append(f"search_string: {DLV['search_term']}")
        elif event != "page_view":
            fields += [
                f"price: {DLV['ecommerce.value']}",
                f"currency: {DLV['ecommerce.currency']}",
                f"item_ids: {DLV['content_ids']}",
                f"number_items: {DLV['num_items']}",
            ]
        if event == "purchase":
            fields.append(f"transaction_id: {DLV['ecommerce.transaction_id']}")
        payload = "{\n    " + ",\n    ".join(fields) + "\n  }"
        b.html_tag(
            f"Snap - {snap_event}",
            f"<script>\n  snaptr('track', '{snap_event}', {payload});\n</script>",
            event,
            setup_tag=base,
            ad_consent=True,
        )


def _add_linkedin(b: ContainerBuilder, cfg: dict[str, Any]) -> None:
    partner_id = b.constant("CONST - LinkedIn Partner ID", str(cfg["partner_id"]))
    base = "LinkedIn - Insight Tag"
    # The Insight Tag records the page view itself once loaded.
    b.html_tag(
        base,
        f"""
<script>
window._linkedin_partner_id = {partner_id};
window._linkedin_data_partner_ids = window._linkedin_data_partner_ids || [];
window._linkedin_data_partner_ids.push(window._linkedin_partner_id);
(function(l) {{
  if (!l) {{ window.lintrk = function(a,b){{window.lintrk.q.push([a,b])}}; window.lintrk.q=[]; }}
  var s = document.getElementsByTagName("script")[0];
  var b = document.createElement("script");
  b.type = "text/javascript"; b.async = true;
  b.src = "https://snap.licdn.com/li.lms-analytics/insight.min.js";
  s.parentNode.insertBefore(b, s);
}})(window.lintrk);
</script>
""",
        "page_view",
        firing="ONCE_PER_LOAD",
        ad_consent=True,
    )
    for event, conversion_id in (cfg.get("conversions") or {}).items():
        if event == "page_view":
            continue
        b.html_tag(
            f"LinkedIn - Conversion - {event}",
            f"<script>\n  window.lintrk('track', {{conversion_id: {int(conversion_id)}, event_id: {DLV['event_id']}}});\n</script>",
            event,
            setup_tag=base,
            ad_consent=True,
        )


_PLATFORM_BUILDERS = {
    "ga4": _add_ga4,
    "google_ads": _add_google_ads,
    "meta": _add_meta,
    "tiktok": _add_tiktok,
    "snapchat": _add_snapchat,
    "linkedin": _add_linkedin,
}


def build_container(config: dict[str, Any]) -> dict[str, Any]:
    store_name = config["store"].get("name") or "Shopify"
    b = ContainerBuilder(
        public_id=config["gtm"]["container_public_id"],
        name=f"{store_name} tracking",
    )
    for key, name in DATA_LAYER_VARIABLES.items():
        b.data_layer_variable(name, key)
    for platform in enabled_platforms(config):
        _PLATFORM_BUILDERS[platform](b, config["platforms"][platform])
    return b.export()


def validate_container(export: dict[str, Any]) -> list[str]:
    """Structural checks that catch mistakes GTM would reject or silently ignore."""
    problems: list[str] = []
    cv = export.get("containerVersion", {})
    tags, triggers, variables = cv.get("tag", []), cv.get("trigger", []), cv.get("variable", [])

    ids = [t["tagId"] for t in tags] + [t["triggerId"] for t in triggers] + [x["variableId"] for x in variables]
    if len(ids) != len(set(ids)):
        problems.append("Duplicate entity IDs")
    for kind, items in (("tag", tags), ("trigger", triggers), ("variable", variables)):
        names = [i["name"] for i in items]
        dupes = {n for n in names if names.count(n) > 1}
        if dupes:
            problems.append(f"Duplicate {kind} names: {sorted(dupes)}")

    trigger_ids = {t["triggerId"] for t in triggers}
    tag_names = {t["name"] for t in tags}
    var_names = {x["name"] for x in variables} | {"_event"}
    for tag in tags:
        for trigger_id in tag.get("firingTriggerId", []):
            if trigger_id not in trigger_ids:
                problems.append(f"Tag '{tag['name']}' fires on missing trigger {trigger_id}")
        for setup in tag.get("setupTag", []):
            if setup["tagName"] not in tag_names:
                problems.append(f"Tag '{tag['name']}' has missing setup tag '{setup['tagName']}'")
        blob = json.dumps(tag["parameter"])
        for ref in _variable_refs(blob):
            if ref not in var_names:
                problems.append(f"Tag '{tag['name']}' references undefined variable {{{{{ref}}}}}")
        if tag["type"] == "html":
            html = next(p["value"] for p in tag["parameter"] if p["key"] == "html")
            for label, pattern in _ES6_PATTERNS.items():
                if re.search(pattern, html):
                    problems.append(f"Tag '{tag['name']}' uses ES6 {label} (GTM Custom HTML is ES5)")
    return problems


_ES6_PATTERNS = {
    "arrow function": r"=>",
    "template literal": r"`",
    "let/const": r"\b(let|const)\s+[A-Za-z_$]",
}


def _variable_refs(text: str) -> set[str]:
    refs, start = set(), 0
    while (i := text.find("{{", start)) != -1:
        j = text.find("}}", i)
        if j == -1:
            break
        refs.add(text[i + 2 : j])
        start = j + 2
    return refs


def summarize_container(export: dict[str, Any]) -> dict[str, Any]:
    cv = export["containerVersion"]
    by_platform: dict[str, list[str]] = {}
    for tag in cv["tag"]:
        platform = tag["name"].split(" - ")[0]
        by_platform.setdefault(platform, []).append(tag["name"])
    return {
        "container": cv["container"]["publicId"],
        "tag_count": len(cv["tag"]),
        "trigger_count": len(cv["trigger"]),
        "variable_count": len(cv["variable"]),
        "tags_by_platform": by_platform,
    }
