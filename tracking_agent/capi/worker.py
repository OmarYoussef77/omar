"""Background worker: sends due jobs, applies consent rules, retries failures."""

from __future__ import annotations

import logging
import threading
import time
from typing import Any

from .event import from_shopify_order
from .platforms import Dispatcher, SendError, SkipPlatform
from .store import Store

log = logging.getLogger("tracking_agent.capi")

MAX_ATTEMPTS = 6
PURGE_INTERVAL = 3600
HEALTH_INTERVAL = 3600


def process_due(store: Store, dispatcher: Dispatcher, config: dict[str, Any], now: float | None = None) -> int:
    """Send every due job once. Returns how many jobs were handled."""
    handled = 0
    for job in store.due_jobs(now):
        order_id, platform = job["order_id"], job["platform"]
        if not store.claim(order_id, platform):
            continue  # another worker took it
        handled += 1
        order, beacon = store.order_and_beacon(order_id)
        if order is None:
            store.finish(order_id, platform, "failed", "order payload missing")
            continue

        event = from_shopify_order(
            order,
            beacon,
            item_id_source=config["store"].get("item_id_source", "variant_id"),
            feed_country=config["store"].get("feed_country", "US"),
            shop_domain=config["store"].get("domain", ""),
        )
        if event.marketing_allowed is False:
            store.finish(order_id, platform, "skipped", "visitor declined marketing consent")
            continue
        if event.marketing_allowed is None and not config["capi"].get("send_without_consent_signal"):
            store.finish(order_id, platform, "skipped", "no consent signal from the browser")
            continue

        try:
            response = dispatcher.send(platform, event)
        except SkipPlatform as exc:
            store.finish(order_id, platform, "skipped", str(exc))
        except SendError as exc:
            if exc.retryable and job["attempts"] + 1 < MAX_ATTEMPTS:
                backoff = 60 * 2 ** job["attempts"]
                store.retry_later(order_id, platform, time.time() + backoff, str(exc))
                log.warning("%s order %s: %s (retrying in %ss)", platform, order_id, exc, backoff)
            else:
                store.finish(order_id, platform, "failed", str(exc))
                log.error("%s order %s failed: %s", platform, order_id, exc)
        except Exception as exc:  # never let one bad order kill the worker
            store.finish(order_id, platform, "failed", f"unexpected: {exc!r}")
            log.exception("%s order %s crashed", platform, order_id)
        else:
            store.finish(order_id, platform, "sent", str(response)[:500])
            log.info("%s order %s sent", platform, order_id)
    return handled


class Worker(threading.Thread):
    def __init__(self, store: Store, dispatcher: Dispatcher, config: dict[str, Any], interval: float = 5.0):
        super().__init__(daemon=True, name="capi-worker")
        self.store, self.dispatcher, self.config, self.interval = store, dispatcher, config, interval
        self._halt = threading.Event()

    def run(self) -> None:
        last_purge = 0.0
        last_health = time.time()  # first check after an hour of data
        while not self._halt.is_set():
            try:
                process_due(self.store, self.dispatcher, self.config)
                if time.time() - last_purge > PURGE_INTERVAL:
                    self.store.purge()
                    last_purge = time.time()
                if time.time() - last_health > HEALTH_INTERVAL:
                    self.health_check()
                    last_health = time.time()
            except Exception:
                log.exception("worker loop error")
            self._halt.wait(self.interval)

    def health_check(self) -> None:
        """Hourly queue health; alerts only when HEALTH_ALERT_WEBHOOK_URL is set."""
        from ..health import AlertThrottle, alert_if_needed, run_health

        webhook = self.dispatcher.secrets.get("health_alert_webhook_url", "")
        if not webhook:
            return
        if not hasattr(self, "_throttle"):
            self._throttle = AlertThrottle(None, float((self.config.get("health") or {}).get("alert_every_hours", 6)))
        overdue = float((self.config.get("health") or {}).get("max_minutes_overdue", 15))
        result = run_health(
            self.config,
            self.dispatcher.secrets,
            report_fetcher=lambda: self.store.report(overdue_minutes=overdue),
        )
        alert_if_needed(result, webhook, self._throttle, self.config["store"].get("name", ""))

    def stop(self) -> None:
        self._halt.set()
