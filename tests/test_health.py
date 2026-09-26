import copy
import json
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from tests.test_capi import SECRETS, _call, _deliver, _run_all, platforms, app, capi_config  # noqa: F401
from tracking_agent import health
from tracking_agent.capi.worker import Worker
from tracking_agent.capi.platforms import Dispatcher


class Recorder:
    """Tiny HTTP server that records requests and returns a canned JSON body."""

    def __init__(self, response=None, status=200):
        self.requests = []
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def _reply(self):
                length = int(self.headers.get("Content-Length") or 0)
                outer.requests.append({"path": self.path, "body": self.rfile.read(length).decode()})
                data = json.dumps(response if response is not None else {}).encode()
                self.send_response(status)
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            do_GET = do_POST = _reply

            def log_message(self, *args):
                pass

        self.server = HTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.url = f"http://127.0.0.1:{self.server.server_port}"

    def close(self):
        self.server.shutdown()


def _report(**overrides):
    base = {
        "window_hours": 24,
        "orders_received": 12,
        "last_order_at": time.time() - 600,
        "platforms": {"meta": {"sent": 12}, "tiktok": {"sent": 12}},
        "overdue": {},
        "recent_problems": [],
    }
    return {**base, **overrides}


# --- queue checks -----------------------------------------------------------


def test_healthy_queue(config):
    findings = health.check_queue(_report(), config)
    assert [f.level for f in findings] == ["ok"]


def test_failure_rate_and_token_hint(config):
    report = _report(
        platforms={"meta": {"sent": 8, "failed": 4}},
        recent_problems=[{"platform": "meta", "status": "failed", "detail": "HTTP 401: OAuthException token expired"}],
    )
    (finding,) = health.check_queue(report, config)
    assert finding.level == "fail" and "4/12" in finding.message and "expired or revoked" in finding.message


def test_google_conversion_not_found_hint(config):
    report = _report(
        platforms={"google_ads": {"failed": 3}},
        recent_problems=[{"platform": "google_ads", "status": "failed", "detail": "Google Ads partial failure: The conversion was not found"}],
    )
    (finding,) = health.check_queue(report, config)
    assert "conversion tag fires on purchase" in finding.message


def test_overdue_jobs_and_config_skips(config):
    report = _report(
        overdue={"tiktok": 3},
        recent_problems=[{"platform": "snapchat", "status": "skipped", "detail": "SNAP_CAPI_ACCESS_TOKEN not set"}],
        platforms={"snapchat": {"skipped": 5}},
    )
    levels = {(f.level, f.check) for f in health.check_queue(report, config)}
    assert ("fail", "queue:tiktok") in levels
    assert ("warn", "queue:snapchat") in levels


def test_consent_skips_are_not_alerts(config):
    report = _report(recent_problems=[{"platform": "meta", "status": "skipped", "detail": "visitor declined marketing consent"}])
    assert [f.level for f in health.check_queue(report, config)] == ["ok"]


def test_webhook_silence(config):
    assert "re-register" in health.check_queue(_report(last_order_at=time.time() - 30 * 3600), config)[0].message
    assert "never received" in health.check_queue(_report(last_order_at=None), config)[0].message


# --- Meta pixel ---------------------------------------------------------------


@pytest.mark.parametrize(
    "response,level,text",
    [
        ({"last_fired_time": "2026-09-26T11:50:00+0000"}, "ok", "min ago"),
        ({"last_fired_time": "2026-09-25T12:00:00+0000"}, "fail", "24.0h ago"),
        ({"is_unavailable": True}, "fail", "unavailable"),
        ({}, "warn", "no record"),
    ],
)
def test_meta_pixel_freshness(config, response, level, text):
    graph = Recorder(response)
    try:
        now = health.datetime(2026, 9, 26, 12, 0, tzinfo=health.timezone.utc).timestamp()
        (finding,) = health.check_meta_pixel(config, "tok", base=graph.url, now=now)
    finally:
        graph.close()
    assert finding.level == level and text in finding.message
    assert graph.requests[0]["path"].startswith("/v23.0/1234567890123456?fields=")


def test_meta_pixel_skipped_without_token(config):
    assert health.check_meta_pixel(config, "") == []


# --- orchestration and alerts ----------------------------------------------------


def test_run_health_combines_checks(capi_config, monkeypatch):
    monkeypatch.setattr(health, "check_meta_pixel", lambda *a, **k: [])
    browse = {"status": "fail", "errors": [], "problems": ["view_item: meta 'ViewContent' did not fire"], "warnings": [], "hit_count": 3}
    result = health.run_health(
        capi_config,
        {},
        synthetic=True,
        report_fetcher=lambda: _report(),
        browse_runner=lambda: browse,
    )
    assert result["status"] == "fail"
    assert any(f["check"] == "synthetic" and "ViewContent" in f["message"] for f in result["findings"])
    text = health.summarize(result, "Example Store")
    assert text.startswith("🚨 Tracking health - Example Store: FAIL") and "ViewContent" in text


def test_run_health_reports_unreachable_server(capi_config, monkeypatch):
    monkeypatch.setattr(health, "check_meta_pixel", lambda *a, **k: [])
    result = health.run_health(capi_config, {})  # no HEALTH_REPORT_TOKEN
    assert result["status"] == "fail"
    assert "HEALTH_REPORT_TOKEN" in result["findings"][0]["message"]


def test_alert_throttle(tmp_path):
    state = tmp_path / "state.json"
    throttle = health.AlertThrottle(state, repeat_hours=6)
    assert not throttle.should_alert("ok", now=1000)  # healthy from the start: stay quiet
    throttle.record("ok", False, now=1000)
    assert throttle.should_alert("fail", now=2000)  # changed -> alert
    throttle.record("fail", True, now=2000)
    reloaded = health.AlertThrottle(state, repeat_hours=6)  # survives between cron runs
    assert not reloaded.should_alert("fail", now=2000 + 3600)  # still failing, too soon
    assert reloaded.should_alert("fail", now=2000 + 6 * 3600)  # reminder
    assert reloaded.should_alert("ok", now=2000 + 3600)  # recovery is announced


def test_send_alert_posts_slack_and_discord_fields(tmp_path):
    hook = Recorder()
    try:
        result = {"status": "warn", "findings": [{"level": "warn", "check": "webhook", "message": "quiet"}]}
        throttle = health.AlertThrottle(tmp_path / "s.json", 6)
        assert health.alert_if_needed(result, hook.url, throttle, "Shop")
        assert not health.alert_if_needed(result, hook.url, throttle, "Shop")  # throttled
    finally:
        hook.close()
    (request,) = hook.requests
    body = json.loads(request["body"])
    assert body["text"] == body["content"] and "quiet" in body["text"]


# --- server report endpoint + worker ------------------------------------------------


def test_report_endpoint_requires_token(app):  # noqa: F811
    assert _call(app, "GET", "/health/report")["status"] == 401  # no token configured
    app.secrets = {**app.secrets, "health_report_token": "hrt"}
    assert _call(app, "GET", "/health/report", headers={"HTTP_AUTHORIZATION": "Bearer wrong"})["status"] == 401
    ok = _call(app, "GET", "/health/report", headers={"HTTP_AUTHORIZATION": "Bearer hrt"})
    assert ok["status"] == 200 and json.loads(ok["body"])["orders_received"] == 0


def test_report_reflects_sends(app, platforms):  # noqa: F811
    platforms.fail["/rest/conversionEvents"] = 400
    _deliver(app)
    _run_all(app, platforms)
    app.secrets = {**app.secrets, "health_report_token": "hrt"}
    response = _call(app, "GET", "/health/report", headers={"HTTP_AUTHORIZATION": "Bearer hrt", "QUERY_STRING": "order_id=9001"})
    report = json.loads(response["body"])
    assert report["orders_received"] == 1
    assert report["platforms"]["meta"] == {"sent": 1}
    assert report["platforms"]["linkedin"] == {"failed": 1}
    assert report["order"]["linkedin"]["status"] == "failed"
    findings = health.check_queue(report, app.config)
    assert any(f.level == "fail" and f.check == "queue:linkedin" for f in findings)


def test_worker_hourly_check_alerts(app, platforms, monkeypatch):  # noqa: F811
    monkeypatch.setattr(health, "check_meta_pixel", lambda *a, **k: [])  # stay offline
    hook = Recorder()
    try:
        platforms.fail["/v23.0/"] = 500
        app.config["capi"]["google_ads"]["delay_seconds"] = 0
        _deliver(app)
        dispatcher = Dispatcher(app.config, {**SECRETS, "health_alert_webhook_url": hook.url}, platforms.endpoints)
        worker = Worker(app.store, dispatcher, app.config)
        # Make every retry immediately due and exhaust them.
        for _ in range(6):
            app.store._exec("UPDATE jobs SET due_at = 0 WHERE status = 'pending'")
            from tracking_agent.capi.worker import process_due

            process_due(app.store, dispatcher, app.config, now=10**10)
        worker.health_check()
        worker.health_check()  # same status again: throttled
    finally:
        hook.close()
    assert len(hook.requests) == 1
    assert "meta" in json.loads(hook.requests[0]["body"])["text"]


def test_wait_for_server_side_ignores_delayed_google():
    calls = []

    def fetch(order_id):
        calls.append(order_id)
        if len(calls) == 1:
            return {"order": {"meta": {"status": "pending"}, "google_ads": {"status": "pending"}}}
        return {"order": {"meta": {"status": "sent"}, "google_ads": {"status": "pending"}}}

    jobs = health.wait_for_server_side(fetch, "9001", timeout=5, interval=0.01)
    assert jobs["meta"]["status"] == "sent" and len(calls) == 2
