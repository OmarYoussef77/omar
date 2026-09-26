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
tracking-agent capi-server               # server-side conversions (see below)
tracking-agent test-checkout             # real-browser tracking test (see below)
tracking-agent health --alert            # health checks, for cron (see below)
```

Options: `--config tracking.yaml` and `--out build`. To use a different model, set `TRACKING_AGENT_MODEL` (the default is `claude-opus-5`, with server-side refusal fallback turned on).

## Server-side conversions (CAPI)

Browser pixels miss buyers who use ad blockers or Safari/iOS tracking protection. With `capi.enabled`, a small server you host picks up those purchases:

```
Thank-you page ── pixel beacon (cookies + consent) ──▶ POST /collect ─┐
Shopify ── orders/paid webhook (HMAC-signed) ───────▶ POST /webhooks/…─┤
                                                                       ▼
                     queue (SQLite, one job per order × platform, retries)
                                                                       ▼
       Meta CAPI · TikTok Events API · Snap CAPI v3 · LinkedIn CAPI · Google Ads enhanced conversions
```

- **No double counting.** The pixel's purchase event and the server's event share the ID `purchase-<order id>`, so each platform keeps one copy. Google Ads enhancements reference the same `orderId` as the browser conversion tag. A test checks that the browser and server IDs match.
- **Better matching.** Email, phone, name and address are normalized and SHA-256 hashed. The server also sends the IP address, user agent, click IDs from the landing URL (`fbclid`, `ttclid`, `ScCid`, `li_fat_id`) and first-party cookies (`_fbp`, `_fbc`, `_ttp`, `_scid`).
- **Consent.** Visitors who decline marketing consent are never sent. Orders with no consent signal from the browser are skipped unless you set `capi.send_without_consent_signal: true`.
- **Reliability.** Duplicate webhook deliveries are ignored. Each platform retries on its own with increasing waits on 429 and 5xx errors. TikTok's error-inside-a-200 responses and Google's partial failures count as failures. `GET /healthz` shows sent, skipped and failed counts. Customer data is deleted after 7 days.
- **Security.** Webhooks must pass Shopify's HMAC signature check. A beacon only counts if its checkout token matches the order's, so nobody can attach cookies to someone else's order.

Setup (the agent walks you through it, and `SETUP.md` lists every step):

| Environment variable | Where to get it |
|---|---|
| `SHOPIFY_WEBHOOK_SECRET`, `SHOPIFY_ADMIN_TOKEN` | Shopify admin > Apps > Develop apps: a custom app with `read_orders` **and protected customer data access** (name, email, phone, address). Without that access, Shopify blanks those fields. |
| `META_CAPI_ACCESS_TOKEN` | Events Manager > dataset > Settings > Conversions API |
| `TIKTOK_EVENTS_ACCESS_TOKEN` | TikTok Events Manager > pixel > Settings |
| `SNAP_CAPI_ACCESS_TOKEN` | Snap Events Manager > pixel > Conversions API |
| `LINKEDIN_CAPI_ACCESS_TOKEN` | LinkedIn developer app with the Conversions API product |
| `GOOGLE_ADS_DEVELOPER_TOKEN`, `GOOGLE_ADS_CLIENT_ID`, `GOOGLE_ADS_CLIENT_SECRET`, `GOOGLE_ADS_REFRESH_TOKEN` | Google Ads API access, with Enhanced conversions turned on for the purchase conversion action |

```bash
tracking-agent capi-server --port 8080         # local
docker build -t tracking-agent . && docker run -p 8080:8080 \
  -v $PWD/tracking.yaml:/app/tracking.yaml -v capi-data:/data \
  -e SHOPIFY_WEBHOOK_SECRET=... -e META_CAPI_ACCESS_TOKEN=... tracking-agent
```

Run it behind HTTPS at `capi.server_url`, as **one process** (the send queue runs inside it). Then register the webhook with the agent's `register_order_webhook` tool and re-paste the regenerated pixel.

**Test before going live.** Set `capi.meta.test_event_code`, `capi.tiktok.test_event_code` and `capi.snapchat.test_mode`, place a test order, and confirm each platform shows the server event deduplicated against the browser one. The request formats follow each platform's public API docs and are covered by tests against fake endpoints. They have not been run against the live APIs, and API versions (`capi.*.api_version`) change over time, so do this test on your own accounts first. Skip any platform whose native Shopify app already sends CAPI.

## Automated test checkout

`tracking-agent test-checkout` drives a real headless Chrome through your store, records every tracking request the browser sends, and checks it against your config:

```
home ─▶ product ─▶ add to cart ─▶ checkout ─▶ (--purchase) pay with Bogus Gateway ─▶ thank-you
```

- **Missing events.** For example, `purchase: tiktok 'CompletePayment' did not fire`.
- **Double counting.** The same purchase event sent twice, e.g. by a leftover theme snippet or a second app.
- **Unexpected pixels.** A pixel ID that isn't in your config, e.g. an old agency pixel.
- **Incomplete events.** Events missing value, currency or product IDs, which catalog ads need.
- **Mismatched event IDs.** A purchase event ID that isn't `purchase-<order id>`, so server-side events can't deduplicate against it.
- **Server-side check.** In `--purchase` mode, it also asks the CAPI server whether that exact order went out to every platform.

**Browse mode (the default) stops at checkout and creates no order**, so it's safe to repeat. `--purchase` places a real test order through Shopify's **Bogus Gateway**, so switch payments to test mode first (Settings > Payments). With a live gateway, the test card is simply declined. Test purchases do send real browser events to your ad platforms, so run them sparingly and preferably while each platform's test-events view is open.

```bash
pip install -e '.[browser]'          # Playwright; uses your installed Chromium
tracking-agent test-checkout         # browse mode
tracking-agent test-checkout --purchase --headed   # full order, watch it run
```

Store behind a password page? Set `STORE_PASSWORD`. Shopify checkout markup changes over time and themes differ, so every selector can be overridden in `checkout_test.selectors`. Shopify may also show bot protection to headless browsers. If it does, run with `--headed` or allow-list the test. To use a specific Chrome build, set `TRACKING_AGENT_CHROMIUM=/path/to/chrome`.

## Ongoing health checks

`tracking-agent health` checks the following, prints a summary and exits with `0` ok / `1` warn / `2` fail:

| Check | Catches |
|---|---|
| Server-side queue (CAPI server `/health/report`) | Send failure rate per platform (with hints for expired tokens and for Google "conversion not found"), stuck jobs, missing tokens or config |
| Webhook silence | No paid orders for `health.max_hours_without_orders`. Shopify quietly deletes webhooks after repeated delivery failures |
| Meta pixel freshness | Graph API `last_fired_time` older than `health.meta_max_hours_since_fired`, or the pixel marked unavailable |
| `--synthetic` | The full browse-mode test checkout |

Alerts go to `HEALTH_ALERT_WEBHOOK_URL` (a Slack or Discord incoming webhook) with `--alert`. You get an alert when the status changes, and a reminder every `health.alert_every_hours` while a problem lasts. The CAPI server also runs the queue checks **hourly by itself** when that variable is set, so the core monitoring needs no scheduler. For the Meta check and the daily browser test, schedule the command: `examples/tracking-health.yml` is a ready-to-use GitHub Actions workflow, or use cron:

```cron
17 * * * *  cd /srv/tracking && tracking-agent health --alert
43 7 * * *  cd /srv/tracking && tracking-agent health --alert --synthetic
```

`/health/report` needs `HEALTH_REPORT_TOKEN`: set the same value on the server and wherever the check runs.

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
| `capi_status` | Shows the server-side setup and which token environment variables are set (never their values) | No |
| `register_order_webhook` | Subscribes the CAPI server to Shopify's `orders/paid` webhook | Yes, **asks first** |
| `run_test_checkout` | Runs a real browser through the store and reports what each platform received | Browse: no. Purchase: places a test order, **asks first** |
| `run_health_check` | Runs the health checks above | No |

## GTM API access (optional)

1. Create a Google Cloud service account and enable the **Tag Manager API**.
2. In GTM > Admin > User Management, add the service account's email with **Publish** permission.
3. In `tracking.yaml`, set `gtm.account_id` and `gtm.container_id` (the numbers in the GTM URL: `.../accounts/<account_id>/containers/<container_id>/`) and `gtm.credentials_file`.

Pushes are paced (one write per second) and retried with exponential backoff on rate limits and server errors, so a large container won't fail halfway. A full push of ~70 entities takes a little over a minute.

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

The browser tests drive real Chromium through a local mock Shopify store (pages send each platform's wire format, answered offline). The tests also run the generated pixel in Node with realistic Shopify events. They also render every GTM Custom HTML tag the way GTM does and check the exact `fbq` / `ttq` / `snaptr` / `lintrk` calls it makes. Other tests cover the GTM API push (against a fake service) and the agent loop (against a fake Messages API).

## Roadmap

- Refund and cancellation events sent server-side.
- More platforms: Pinterest, Reddit, X, Microsoft UET.
