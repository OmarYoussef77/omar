"""Ongoing tracking health checks with alerting.

Checks:
  * Server-side queue (from the CAPI server's /health/report): failure rate
    per platform, stuck jobs, config gaps, and silence (no paid orders for
    too long usually means Shopify removed the webhook after failures).
  * Meta pixel freshness via the Graph API (last_fired_time).
  * Optional synthetic browse (tracking-agent test-checkout in browse mode):
    the full browser pipeline, without placing an order.

Run it from cron / a scheduler: `tracking-agent health --alert`. The CAPI
server also runs the queue checks hourly by itself when
HEALTH_ALERT_WEBHOOK_URL is set.
"""

from __future__ import annotations

import json
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

LEVELS = {"ok": 0, "warn": 1, "fail": 2}


@dataclass
class Finding:
    level: str  # ok | warn | fail
    check: str
    message: str


# --- server-side queue --------------------------------------------------------


def fetch_report(server_url: str, token: str, order_id: str | None = None, hours: float = 24) -> dict[str, Any]:
    query = {"hours": hours, **({"order_id": order_id} if order_id else {})}
    url = f"{server_url.rstrip('/')}/health/report?{urllib.parse.urlencode(query)}"
    request = urllib.request.Request(url, headers={"Authorization": f"Bearer {token}"})
    with urllib.request.urlopen(request, timeout=20) as response:
        return json.loads(response.read())


def check_queue(report: dict[str, Any], config: dict[str, Any], now: float | None = None) -> list[Finding]:
    now = now or time.time()
    health = config.get("health") or {}
    max_rate = float(health.get("max_failure_rate", 0.05))
    findings: list[Finding] = []

    for platform, count in (report.get("overdue") or {}).items():
        findings.append(
            Finding("fail", f"queue:{platform}", f"{count} {platform} job(s) overdue - the CAPI worker is stuck or down")
        )

    problems = report.get("recent_problems") or []
    for platform, counts in sorted((report.get("platforms") or {}).items()):
        sent, failed = counts.get("sent", 0), counts.get("failed", 0)
        attempted = sent + failed
        if attempted and failed / attempted > max_rate:
            latest = next((p["detail"] for p in problems if p["platform"] == platform and p["status"] == "failed"), "")
            hint = ""
            if platform == "google_ads" and "not found" in latest.lower():
                hint = " Google can't find the browser conversion to enhance: check the Google Ads conversion tag fires on purchase."
            if any(code in latest for code in ("HTTP 401", "HTTP 403", "OAuthException", "expired")):
                hint = " The access token looks expired or revoked: generate a new one."
            findings.append(
                Finding(
                    "fail",
                    f"queue:{platform}",
                    f"{platform}: {failed}/{attempted} server-side sends failed in the last "
                    f"{report.get('window_hours', 24):g}h. Latest error: {latest[:200]}.{hint}",
                )
            )
        config_skips = [
            p for p in problems
            if p["platform"] == platform and p["status"] == "skipped" and ("not set" in (p["detail"] or "") or "missing" in (p["detail"] or ""))
        ]
        if config_skips:
            findings.append(
                Finding("warn", f"queue:{platform}", f"{platform}: orders skipped - {config_skips[0]['detail']}")
            )

    max_hours = float(health.get("max_hours_without_orders", 24))
    last = report.get("last_order_at")
    if last is None:
        findings.append(Finding("warn", "webhook", "The CAPI server has never received a paid order. Is the orders/paid webhook registered?"))
    elif now - last > max_hours * 3600:
        findings.append(
            Finding(
                "warn",
                "webhook",
                f"No paid orders received for {(now - last) / 3600:.0f}h. If the store did have sales, Shopify has "
                "probably removed the orders/paid webhook after delivery failures: re-register it.",
            )
        )
    if not findings:
        findings.append(Finding("ok", "queue", f"{report.get('orders_received', 0)} orders in the last {report.get('window_hours', 24):g}h, sends healthy"))
    return findings


# --- Meta pixel --------------------------------------------------------------


def check_meta_pixel(config: dict[str, Any], token: str, base: str = "https://graph.facebook.com", now: float | None = None) -> list[Finding]:
    meta = config["platforms"].get("meta") or {}
    if not (meta.get("enabled") and token):
        return []
    version = (config.get("capi") or {}).get("meta", {}).get("api_version", "v23.0")
    query = urllib.parse.urlencode({"fields": "name,last_fired_time,is_unavailable", "access_token": token})
    try:
        with urllib.request.urlopen(f"{base}/{version}/{meta['pixel_id']}?{query}", timeout=20) as response:
            data = json.loads(response.read())
    except urllib.error.HTTPError as exc:
        return [Finding("warn", "meta_pixel", f"Couldn't read the Meta pixel (HTTP {exc.code}); check the token's permissions")]
    except urllib.error.URLError as exc:
        return [Finding("warn", "meta_pixel", f"Couldn't reach the Graph API: {exc.reason}")]
    if data.get("is_unavailable"):
        return [Finding("fail", "meta_pixel", f"Meta reports pixel {meta['pixel_id']} as unavailable")]
    fired = data.get("last_fired_time")
    if not fired:
        return [Finding("warn", "meta_pixel", "Meta has no record of the pixel firing")]
    age = (now or time.time()) - datetime.fromisoformat(fired.replace("Z", "+00:00").replace("+0000", "+00:00")).timestamp()
    max_hours = float((config.get("health") or {}).get("meta_max_hours_since_fired", 6))
    if age > max_hours * 3600:
        return [Finding("fail", "meta_pixel", f"Meta pixel last fired {age / 3600:.1f}h ago - browser tracking may be broken")]
    return [Finding("ok", "meta_pixel", f"Meta pixel last fired {age / 60:.0f} min ago")]


# --- synthetic browse ----------------------------------------------------------


def check_synthetic(report: dict[str, Any]) -> list[Finding]:
    if report["status"] == "pass":
        return [Finding("ok", "synthetic", f"Browse test passed ({report['hit_count']} tracking requests)")]
    findings = [Finding("fail", "synthetic", p) for p in report.get("errors", []) + report.get("problems", [])]
    findings += [Finding("warn", "synthetic", w) for w in report.get("warnings", [])]
    return findings


# --- orchestration --------------------------------------------------------------


def run_health(
    config: dict[str, Any],
    secrets: dict[str, str],
    *,
    synthetic: bool = False,
    report_fetcher: Callable[[], dict[str, Any]] | None = None,
    browse_runner: Callable[[], dict[str, Any]] | None = None,
) -> dict[str, Any]:
    findings: list[Finding] = []
    capi = config.get("capi") or {}
    if capi.get("enabled"):
        try:
            if report_fetcher is None:
                if not secrets.get("health_report_token"):
                    raise RuntimeError("HEALTH_REPORT_TOKEN is not set")
                report = fetch_report(capi["server_url"], secrets["health_report_token"])
            else:
                report = report_fetcher()
            findings += check_queue(report, config)
        except Exception as exc:
            findings.append(Finding("fail", "capi_server", f"Couldn't get the CAPI server's health report: {exc}"))
    findings += check_meta_pixel(config, secrets.get("meta", ""))
    if synthetic:
        try:
            if browse_runner is None:
                from .checkout_test.runner import run_checkout_test

                browse_report = run_checkout_test(config, purchase=False)
            else:
                browse_report = browse_runner()
            findings += check_synthetic(browse_report)
        except Exception as exc:
            findings.append(Finding("fail", "synthetic", f"Browse test couldn't run: {exc}"))

    status = max((f.level for f in findings), key=LEVELS.get, default="ok")
    return {
        "status": status,
        "checked_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "findings": [asdict(f) for f in findings],
    }


def summarize(result: dict[str, Any], store_name: str = "") -> str:
    icon = {"ok": "✅", "warn": "⚠️", "fail": "🚨"}
    lines = [f"{icon[result['status']]} Tracking health{f' - {store_name}' if store_name else ''}: {result['status'].upper()}"]
    for finding in result["findings"]:
        if finding["level"] != "ok":
            lines.append(f"{icon[finding['level']]} [{finding['check']}] {finding['message']}")
    return "\n".join(lines)


# --- alerting -------------------------------------------------------------------


class AlertThrottle:
    """Alert when status changes, and repeat an ongoing problem at most every N hours."""

    def __init__(self, path: Path | None, repeat_hours: float):
        self.path = path
        self.repeat = repeat_hours * 3600
        self.state = {"status": "ok", "alerted_at": 0.0}
        if path and path.exists():
            try:
                self.state = json.loads(path.read_text())
            except ValueError:
                pass

    def should_alert(self, status: str, now: float | None = None) -> bool:
        now = now or time.time()
        changed = status != self.state.get("status")
        repeat = status != "ok" and now - self.state.get("alerted_at", 0) >= self.repeat
        return changed or repeat

    def record(self, status: str, alerted: bool, now: float | None = None) -> None:
        self.state["status"] = status
        if alerted:
            self.state["alerted_at"] = now or time.time()
        if self.path:
            self.path.write_text(json.dumps(self.state))


def send_alert(webhook_url: str, text: str) -> None:
    # "text" is read by Slack/Google Chat/Mattermost, "content" by Discord.
    body = json.dumps({"text": text, "content": text}).encode()
    request = urllib.request.Request(webhook_url, data=body, headers={"Content-Type": "application/json"}, method="POST")
    urllib.request.urlopen(request, timeout=20).close()


def alert_if_needed(result: dict[str, Any], webhook_url: str, throttle: AlertThrottle, store_name: str = "") -> bool:
    status = result["status"]
    alerted = False
    if webhook_url and throttle.should_alert(status):
        send_alert(webhook_url, summarize(result, store_name))
        alerted = True
    throttle.record(status, alerted)
    return alerted


# --- after a test purchase --------------------------------------------------------


def wait_for_server_side(
    fetcher: Callable[[str], dict[str, Any]], order_id: str, timeout: float, interval: float = 5.0
) -> dict[str, Any]:
    """Poll the CAPI server until every job for the order has left 'pending'
    (Google Ads enhancements are deliberately delayed, so they may stay pending)."""
    deadline = time.time() + timeout
    jobs: dict[str, Any] = {}
    while True:
        jobs = fetcher(order_id).get("order") or {}
        waiting = [p for p, j in jobs.items() if j["status"] in ("pending", "sending") and p != "google_ads"]
        if jobs and not waiting:
            break
        if time.time() >= deadline:
            break
        time.sleep(interval)
    return jobs


def verify_order_server_side(config: dict[str, Any], secrets: dict[str, str], order_id: str) -> dict[str, Any]:
    """After a test purchase: did the CAPI server send this order to every platform?"""
    capi = config.get("capi") or {}
    if not capi.get("enabled"):
        return {"skipped": "capi is not enabled"}
    token = secrets.get("health_report_token")
    if not token:
        return {"skipped": "set HEALTH_REPORT_TOKEN to verify server-side sends"}
    timeout = float(capi.get("wait_seconds", 60)) + 90
    jobs = wait_for_server_side(lambda oid: fetch_report(capi["server_url"], token, order_id=oid, hours=1), order_id, timeout)
    if not jobs:
        return {"status": "fail", "detail": f"the CAPI server never received order {order_id} (is the webhook registered?)", "jobs": {}}
    bad = {p: j for p, j in jobs.items() if j["status"] in ("failed", "skipped")}
    return {"status": "fail" if bad else "pass", "jobs": jobs}
