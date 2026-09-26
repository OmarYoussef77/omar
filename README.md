# Tracking Agent

A Claude-powered agent that sets up conversion tracking for a **Shopify** store through **Google Tag Manager**, covering **GA4, Google Ads, Meta, TikTok, Snapchat and LinkedIn**.

You chat with it. It collects your IDs, checks your live store for tracking that's already installed, builds the tracking files, and pushes them to GTM. It asks you before every GTM change.

```
you>   Set up tracking for mystore.com. Meta pixel is 1234567890123456, TikTok is C4AB...
agent> [get_config] [set_config] [audit_site]
       Your Meta pixel is already firing from the Facebook & Instagram app. Keep the app
       (it also sends server-side CAPI) and skip Meta in GTM, or let GTM own it?
       ...
agent> [generate_tracking_files]  41 tags, 10 triggers, 18 variables. No problems found.
agent> [push_to_gtm]
       ======================================================================
       APPROVAL NEEDED
       Write to GTM workspace 'tracking-agent setup': create 69 ...
       Proceed? [y/N]
```

## How it works

```
Shopify storefront + checkout
  └─ Custom Pixel (Settings > Customer events)     ← shopify-custom-pixel.js
       • subscribes to Shopify standard events (incl. checkout & purchase)
       • pushes GA4-schema ecommerce events + event_id + user_data to the dataLayer
       • Consent Mode v2 from Shopify's customer privacy API
       • loads GTM inside the pixel sandbox
            └─ GTM container                        ← gtm-container.json
                 GA4 · Google Ads · Meta · TikTok · Snapchat · LinkedIn tags
```

| Shopify event | dataLayer | GA4 | Google Ads | Meta | TikTok | Snapchat | LinkedIn |
|---|---|---|---|---|---|---|---|
| page_viewed | page_view | Google tag | Google tag + linker | PageView | page() | PAGE_VIEW | Insight Tag |
| collection_viewed | view_item_list | ✓ | if configured | – | – | LIST_VIEW | if configured |
| product_viewed | view_item | ✓ | if configured | ViewContent | ViewContent | VIEW_CONTENT | if configured |
| search_submitted | search | ✓ | if configured | Search | Search | SEARCH | if configured |
| product_added_to_cart | add_to_cart | ✓ | if configured | AddToCart | AddToCart | ADD_CART | if configured |
| cart_viewed | view_cart | ✓ | if configured | – | – | – | if configured |
| checkout_started | begin_checkout | ✓ | if configured | InitiateCheckout | InitiateCheckout | START_CHECKOUT | if configured |
| checkout_shipping_info_submitted | add_shipping_info | ✓ | if configured | – | – | – | if configured |
| payment_info_submitted | add_payment_info | ✓ | if configured | AddPaymentInfo | AddPaymentInfo | ADD_BILLING | if configured |
| checkout_completed | purchase | ✓ | if configured | Purchase | CompletePayment | PURCHASE | if configured |

Claude handles the conversation, the decisions and the checks. The files come from plain Python code, not from Claude, so the GTM JSON and pixel code come out the same way every time and are tested. Every event carries Shopify's event ID (`eventID` / `event_id` / `client_dedup_id`), so a server-side Conversions API feed can deduplicate against it.

## Quick start

```bash
python -m venv .venv && . .venv/bin/activate
pip install -e '.[gtm]'          # drop [gtm] if you'll import the container by hand
export ANTHROPIC_API_KEY=sk-ant-...

tracking-agent                   # chat; writes tracking.yaml and build/
```

Other commands (no LLM needed):

```bash
cp tracking.example.yaml tracking.yaml   # fill in your IDs
tracking-agent generate                  # -> build/gtm-container.json, shopify-custom-pixel.js, SETUP.md
tracking-agent audit https://mystore.com # what's already installed + conflicts
```

Options: `--config tracking.yaml` and `--out build`. To use a different model, set `TRACKING_AGENT_MODEL` (the default is `claude-opus-5`, with server-side refusal fallback turned on).

## Agent tools

| Tool | What it does | Writes externally? |
|---|---|---|
| `get_config` / `set_config` | Reads and edits `tracking.yaml`, then checks the ID formats | No (local file) |
| `audit_site` | Scans the storefront for GTM, GA4, AW-, fbq, ttq, snaptr and LinkedIn tags, including Shopify app pixels, and flags double-counting | No |
| `plan_events` | Shows the event mapping table above for your enabled platforms | No |
| `generate_tracking_files` | Builds and checks the container, the pixel and the checklist | No (local files) |
| `push_to_gtm` | Adds or updates tags, triggers and variables in a GTM **workspace** (a draft) | Yes, **asks first** |
| `create_gtm_version` | Saves the workspace as a new version (not live yet) | Yes, **asks first** |
| `publish_gtm_version` | Makes the version live | Yes, **asks first** |

## GTM API access (optional)

1. Create a Google Cloud service account and enable the **Tag Manager API**.
2. In GTM > Admin > User Management, add the service account's email with **Publish** permission.
3. In `tracking.yaml`, set `gtm.account_id` and `gtm.container_id` (the numbers in the GTM URL: `.../accounts/<account_id>/containers/<container_id>/`) and `gtm.credentials_file`.

If you skip this, import `build/gtm-container.json` yourself: GTM > Admin > Import Container > New workspace > **Merge**.

## Things to know

- **One source per platform.** If a Shopify sales channel app (Facebook & Instagram, TikTok, Snapchat, Google & YouTube) already fires a pixel, adding the same pixel through GTM double-counts conversions. The audit catches this. In most cases, keep the app, because it also sends server-side CAPI.
- **Product IDs must match your catalog feeds.** Set `store.item_id_source` to `variant_id`, `product_id`, `sku` or `shopify_feed` (`shopify_US_<product>_<variant>`, the format Shopify's feed apps use).
- **The pixel runs in a sandboxed iframe.** GA4 gets the real page URL and title from the dataLayer. Other pixels may log the sandbox URL, but events, values and IDs are correct.
- **Shopify has no API for custom pixels**, so you paste `shopify-custom-pixel.js` into Shopify yourself. `SETUP.md` walks you through it, along with QA for each platform.

## Development

```bash
pip install -e '.[dev]'
pytest
```

The tests run the generated pixel in Node with realistic Shopify events. They also render every GTM Custom HTML tag the way GTM does and check the exact `fbq` / `ttq` / `snaptr` / `lintrk` calls it makes. Other tests cover the GTM API push (against a fake service) and the agent loop (against a fake Messages API).

## Roadmap

- Server-side Conversions API relay (Shopify `orders/paid` webhook → Meta CAPI / TikTok Events API / Snap CAPI / LinkedIn CAPI / Google Ads enhanced conversions), deduplicated on `event_id`.
- More platforms: Pinterest, Reddit, X, Microsoft UET.
- A headless-browser checker that places a test order and confirms each platform received the events.
