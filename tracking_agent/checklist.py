"""Setup/QA checklist for the steps no API can do (or should do) for you."""

from __future__ import annotations

from typing import Any

from .config import enabled_platforms

_PLATFORM_QA = {
    "ga4": (
        "GA4",
        "GA4 > Reports > Realtime. Confirm page_view, view_item, add_to_cart, "
        "begin_checkout and purchase arrive with items[] and value. Mark `purchase` as a key event.",
    ),
    "google_ads": (
        "Google Ads",
        "Tag Assistant (tagassistant.google.com) shows the conversion tag firing on purchase with value, "
        "currency and transaction ID. In Google Ads > Goals, the conversion status moves to 'Recording' "
        "within ~24h. Turn on Enhanced Conversions for web if you want better match rates (next step below).",
    ),
    "meta": (
        "Meta",
        "Events Manager > your dataset > Test events. Browse, add to cart and buy: you should see PageView, "
        "ViewContent, AddToCart, InitiateCheckout, AddPaymentInfo, Purchase with content_ids matching your "
        "catalog. If CAPI also runs (Facebook & Instagram app), check the 'Deduplicated' column.",
    ),
    "tiktok": (
        "TikTok",
        "TikTok Events Manager > your pixel > Test Events, or the TikTok Pixel Helper extension. "
        "Confirm ViewContent, AddToCart, InitiateCheckout, CompletePayment with contents[] and value.",
    ),
    "snapchat": (
        "Snapchat",
        "Snap Events Manager > Test events, or the Snap Pixel Helper extension. Confirm PAGE_VIEW, "
        "VIEW_CONTENT, ADD_CART, START_CHECKOUT, PURCHASE with item_ids and price.",
    ),
    "linkedin": (
        "LinkedIn",
        "Campaign Manager > Analyze > Insight Tag shows the domain as active (can take 24h). Each "
        "conversion shows 'Active' after its first event.",
    ),
}


def build_checklist(config: dict[str, Any], audit_warnings: list[str] | None = None) -> str:
    gtm_id = config["gtm"]["container_public_id"]
    platforms = enabled_platforms(config)
    lines = [
        f"# Tracking setup - {config['store'].get('name') or 'your store'}",
        "",
        f"GTM container: `{gtm_id}` | Platforms: {', '.join(platforms)}",
        "",
    ]
    if audit_warnings:
        lines += ["## ⚠️ Resolve before going live", ""]
        lines += [f"- [ ] {w}" for w in audit_warnings]
        lines.append("")

    lines += [
        "## 1. Remove old/duplicate tracking",
        "",
        "- [ ] Remove any hard-coded GTM / gtag / fbq / ttq / snaptr / LinkedIn snippets from "
        "`theme.liquid` and the old *Additional scripts* box (it stops working after checkout "
        "upgrade anyway).",
        "- [ ] For each platform, choose ONE browser-side source: the native Shopify app's pixel "
        "**or** these GTM tags. Keeping both double-counts conversions. The native apps "
        "(Facebook & Instagram, TikTok, Snapchat) also send server-side Conversions API events - "
        "if you keep their pixel, delete that platform's tags from the GTM workspace before "
        "publishing.",
        "",
        "## 2. Install the Shopify custom pixel",
        "",
        "- [ ] Shopify admin > Settings > Customer events > **Add custom pixel** > name it `GTM`.",
        "- [ ] Paste `shopify-custom-pixel.js`.",
        "- [ ] Customer privacy: set *Permission* to **Required** with *Marketing* and *Analytics* "
        "(or follow your legal advice). The pixel also sends Google Consent Mode v2 signals.",
        "- [ ] Data sale: *Data collected does not qualify as data sale* unless advised otherwise.",
        "- [ ] Save, then **Connect**.",
        "",
        "## 3. Load the GTM container",
        "",
        "- [ ] Either let the agent push it (creates a workspace, you review before publishing), or: "
        "GTM > Admin > Import Container > `gtm-container.json` > *New workspace* > **Merge** > "
        "*Rename conflicting tags*.",
        "- [ ] Preview the workspace. Note: GTM Preview can't attach to the pixel sandbox directly; "
        "use the Shopify Pixel Helper browser extension plus each platform's test tool below.",
        "- [ ] Submit / publish the version.",
        "",
        "## 4. QA each platform",
        "",
        "- [ ] Run `tracking-agent test-checkout` (browse mode, no order) and fix every problem it reports. "
        "Then, with payments in test mode, `tracking-agent test-checkout --purchase`.",
    ]
    for platform in platforms:
        name, qa = _PLATFORM_QA[platform]
        lines.append(f"- [ ] **{name}** - {qa}")
    if config.get("capi", {}).get("enabled"):
        lines += ["", *_capi_steps(config)]
    lines += [
        "",
        "## 5. Known limits of pixel-sandbox tracking",
        "",
        "- Tags run inside Shopify's sandboxed iframe. GA4 gets the real page URL/title from the "
        "dataLayer (already wired); other pixels may report the sandbox URL in their URL field. "
        "Events, values and IDs are unaffected.",
        "- Browser pixels miss ad-blocked and iOS-restricted users. For Meta, TikTok and Snapchat "
        "the native Shopify apps add server-side Conversions API; for Google Ads, enable "
        "Enhanced Conversions; for LinkedIn, set up the Conversions API in Campaign Manager. "
        "Events carry `event_id` so a server-side feed can deduplicate against them.",
        f"- Product IDs use `{config['store']['item_id_source']}`. They must match the IDs in "
        "your catalogs/feeds (Meta catalog, TikTok catalog, Snap catalog, Merchant Center) or "
        "dynamic/retargeting ads won't match products.",
        "",
    ]
    return "\n".join(lines)


def _capi_steps(config: dict[str, Any]) -> list[str]:
    from .capi.server import capi_platforms
    from .capi.shopify_admin import webhook_uri

    capi = config["capi"]
    platforms = capi_platforms(config)
    env = {
        "meta": "META_CAPI_ACCESS_TOKEN (Events Manager > dataset > Settings > Conversions API > Generate access token)",
        "tiktok": "TIKTOK_EVENTS_ACCESS_TOKEN (TikTok Events Manager > pixel > Settings > Generate Access Token)",
        "snapchat": "SNAP_CAPI_ACCESS_TOKEN (Snap Events Manager > pixel > Conversions API token)",
        "linkedin": "LINKEDIN_CAPI_ACCESS_TOKEN (LinkedIn developer app with the Conversions API product; the purchase conversion's data source must include CAPI)",
        "google_ads": "GOOGLE_ADS_DEVELOPER_TOKEN, GOOGLE_ADS_CLIENT_ID, GOOGLE_ADS_CLIENT_SECRET, GOOGLE_ADS_REFRESH_TOKEN (turn on Enhanced conversions for the purchase conversion action)",
    }
    lines = [
        "## 4b. Server-side conversions",
        "",
        f"Platforms sent server-side: {', '.join(platforms) or 'none'}",
        "",
        "- [ ] Shopify admin > Settings > Apps > Develop apps > create an app with the "
        "`read_orders` Admin API scope. Install it and note the Admin API token "
        "(SHOPIFY_ADMIN_TOKEN) and the API secret key (SHOPIFY_WEBHOOK_SECRET).",
        "- [ ] In the app's API access settings, request **protected customer data** access for "
        "name, email, phone and address. Without it Shopify blanks those fields in the order "
        "webhook and match rates drop sharply.",
        "- [ ] Set the access tokens as environment variables on the server:",
        *[f"  - {env[p]}" for p in platforms],
        f"- [ ] Deploy `tracking-agent capi-server` behind HTTPS at `{capi.get('server_url') or '<capi.server_url>'}` "
        "(Dockerfile included). Keep it to one process: the send queue runs inside it.",
        f"- [ ] Register the webhook (the agent's register_order_webhook tool): `{webhook_uri(capi.get('server_url') or 'https://<server>')}`.",
        "- [ ] Re-paste the regenerated `shopify-custom-pixel.js` - it now sends the purchase beacon to the server.",
        "- [ ] Test first: set capi.meta.test_event_code / capi.tiktok.test_event_code / capi.snapchat.test_mode, "
        "place a test order, and confirm each platform shows the server event deduplicated against the browser one "
        "(same event ID `purchase-<order id>`). Then clear the test settings.",
        f"- [ ] Consent: orders without a consent signal from the browser are "
        f"{'SENT (send_without_consent_signal is on)' if capi.get('send_without_consent_signal') else 'skipped'}. "
        "Visitors who decline marketing consent are never sent.",
        "- [ ] Check `GET /healthz` for sent / skipped / failed counts per platform.",
        "- [ ] Monitoring: set HEALTH_REPORT_TOKEN and HEALTH_ALERT_WEBHOOK_URL (Slack/Discord) on the server "
        "(it then checks its queue hourly), and schedule `tracking-agent health --alert` "
        "(see examples/tracking-health.yml).",
    ]
    return lines
