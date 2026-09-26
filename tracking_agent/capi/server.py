"""WSGI app for server-side conversions.

  POST /webhooks/shopify/orders-paid   Shopify orders/paid webhook (HMAC-verified)
  POST /collect                         beacon from the custom pixel on the thank-you page
  GET  /healthz                         liveness + job counts

Run locally with `tracking-agent capi-server`, or in production with any
WSGI server, e.g. `gunicorn -w 1 --threads 4 'tracking_agent.capi.server:create_app()'`
(one process: the background worker lives inside it).
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import os
import time
from typing import Any, Callable, Iterable

from ..config import CAPI_PLATFORMS, enabled_platforms, load_config
from .event import order_number
from .platforms import Dispatcher, load_secrets
from .store import Store
from .worker import Worker

log = logging.getLogger("tracking_agent.capi")

MAX_WEBHOOK_BYTES = 5 * 1024 * 1024
MAX_BEACON_BYTES = 8 * 1024

# Only these beacon fields are kept; everything else is dropped.
BEACON_COOKIES = ("_fbp", "_fbc", "_ttp", "ttclid", "_scid", "li_fat_id")

StartResponse = Callable[[str, list[tuple[str, str]]], Any]


def verify_shopify_hmac(body: bytes, header: str, secret: str) -> bool:
    if not (secret and header):
        return False
    digest = hmac.new(secret.encode(), body, hashlib.sha256).digest()
    return hmac.compare_digest(base64.b64encode(digest).decode(), header)


def capi_platforms(config: dict[str, Any]) -> list[str]:
    return [p for p in enabled_platforms(config) if p in CAPI_PLATFORMS]


def clean_beacon(raw: dict[str, Any]) -> dict[str, Any]:
    cookies = raw.get("cookies") if isinstance(raw.get("cookies"), dict) else {}
    return {
        "marketing_allowed": raw.get("marketing_allowed") is True,
        "page_url": str(raw.get("page_url", ""))[:2000],
        "user_agent": str(raw.get("user_agent", ""))[:1000],
        "cookies": {k: str(cookies[k])[:500] for k in BEACON_COOKIES if cookies.get(k)},
    }


class CapiApp:
    def __init__(self, config: dict[str, Any], store: Store, secrets: dict[str, str]):
        self.config = config
        self.store = store
        self.secrets = secrets

    def __call__(self, environ: dict[str, Any], start_response: StartResponse) -> Iterable[bytes]:
        method = environ.get("REQUEST_METHOD", "GET")
        path = environ.get("PATH_INFO", "")
        try:
            if path == "/webhooks/shopify/orders-paid" and method == "POST":
                return self._orders_paid(environ, start_response)
            if path == "/collect" and method == "POST":
                return self._collect(environ, start_response)
            if path == "/collect" and method == "OPTIONS":
                return self._respond(start_response, "204 No Content", b"", cors=True)
            if path == "/health/report" and method == "GET":
                return self._health_report(environ, start_response)
            if path == "/healthz" and method == "GET":
                body = json.dumps({"ok": True, "jobs": self.store.stats()}).encode()
                return self._respond(start_response, "200 OK", body, content_type="application/json")
            return self._respond(start_response, "404 Not Found", b"not found")
        except Exception:
            log.exception("request failed: %s %s", method, path)
            return self._respond(start_response, "500 Internal Server Error", b"error")

    # -- handlers ------------------------------------------------------------
    def _orders_paid(self, environ: dict[str, Any], start_response: StartResponse) -> Iterable[bytes]:
        body = self._read(environ, MAX_WEBHOOK_BYTES)
        if body is None:
            return self._respond(start_response, "413 Payload Too Large", b"too large")
        header = environ.get("HTTP_X_SHOPIFY_HMAC_SHA256", "")
        if not verify_shopify_hmac(body, header, self.secrets.get("shopify_webhook_secret", "")):
            return self._respond(start_response, "401 Unauthorized", b"bad hmac")

        order = json.loads(body)
        order_id = order_number(order.get("id") or order.get("admin_graphql_api_id"))
        if not order_id:
            return self._respond(start_response, "400 Bad Request", b"no order id")
        now = time.time()
        capi = self.config["capi"]
        jobs = {
            p: now + (capi["google_ads"]["delay_seconds"] if p == "google_ads" else capi["wait_seconds"])
            for p in capi_platforms(self.config)
        }
        is_new = self.store.add_order(order_id, order.get("checkout_token"), order, jobs)
        log.info("orders/paid %s (%s)", order_id, "queued" if is_new else "duplicate delivery ignored")
        # Always 200 quickly: Shopify retries (and eventually drops the
        # subscription) on slow or failed responses.
        return self._respond(start_response, "200 OK", b"ok")

    def _collect(self, environ: dict[str, Any], start_response: StartResponse) -> Iterable[bytes]:
        body = self._read(environ, MAX_BEACON_BYTES)
        if body is None:
            return self._respond(start_response, "413 Payload Too Large", b"", cors=True)
        try:
            raw = json.loads(body)
        except ValueError:
            return self._respond(start_response, "400 Bad Request", b"", cors=True)
        order_id = order_number(raw.get("order_id")) if isinstance(raw, dict) else ""
        if not order_id:
            return self._respond(start_response, "400 Bad Request", b"", cors=True)
        data = clean_beacon(raw)
        data["ip"] = self._client_ip(environ)
        self.store.add_beacon(order_id, str(raw.get("checkout_token") or "") or None, data)
        return self._respond(start_response, "204 No Content", b"", cors=True)

    def _health_report(self, environ: dict[str, Any], start_response: StartResponse) -> Iterable[bytes]:
        token = self.secrets.get("health_report_token", "")
        supplied = environ.get("HTTP_AUTHORIZATION", "").removeprefix("Bearer ").strip()
        if not token or not hmac.compare_digest(supplied, token):
            return self._respond(start_response, "401 Unauthorized", b"set HEALTH_REPORT_TOKEN and send it as a Bearer token")
        from urllib.parse import parse_qs

        query = parse_qs(environ.get("QUERY_STRING", ""))
        health = self.config.get("health") or {}
        report = self.store.report(
            window_hours=float(query.get("hours", ["24"])[0]),
            overdue_minutes=float(health.get("max_minutes_overdue", 15)),
            order_id=order_number(query.get("order_id", [""])[0]) or None,
        )
        return self._respond(start_response, "200 OK", json.dumps(report).encode(), content_type="application/json")

    # -- helpers -------------------------------------------------------------
    @staticmethod
    def _read(environ: dict[str, Any], limit: int) -> bytes | None:
        try:
            length = int(environ.get("CONTENT_LENGTH") or 0)
        except ValueError:
            length = 0
        if length > limit:
            return None
        return environ["wsgi.input"].read(length) if length else b""

    @staticmethod
    def _client_ip(environ: dict[str, Any]) -> str:
        forwarded = environ.get("HTTP_X_FORWARDED_FOR", "")
        return forwarded.split(",")[0].strip() if forwarded else environ.get("REMOTE_ADDR", "")

    @staticmethod
    def _respond(
        start_response: StartResponse,
        status: str,
        body: bytes,
        *,
        content_type: str = "text/plain",
        cors: bool = False,
    ) -> list[bytes]:
        headers = [("Content-Type", content_type), ("Content-Length", str(len(body)))]
        if cors:
            # The custom pixel runs in a sandboxed iframe (origin may be
            # "null"); the beacon carries no credentials, so * is safe.
            headers += [
                ("Access-Control-Allow-Origin", "*"),
                ("Access-Control-Allow-Methods", "POST, OPTIONS"),
                ("Access-Control-Allow-Headers", "Content-Type"),
            ]
        start_response(status, headers)
        return [body]


def create_app(config_path: str | None = None, *, start_worker: bool = True, endpoints: dict[str, str] | None = None) -> CapiApp:
    config = load_config(config_path or os.environ.get("TRACKING_CONFIG", "tracking.yaml"))
    secrets = load_secrets()
    if not secrets["shopify_webhook_secret"]:
        raise RuntimeError("SHOPIFY_WEBHOOK_SECRET is not set (your Shopify app's API secret key)")
    store = Store(config["capi"].get("database", "capi.sqlite3"))
    app = CapiApp(config, store, secrets)
    if start_worker:
        Worker(store, Dispatcher(config, secrets, endpoints), config).start()
    return app


def serve(config_path: str, host: str = "0.0.0.0", port: int = 8080) -> None:
    from socketserver import ThreadingMixIn
    from wsgiref.simple_server import WSGIServer, make_server

    class ThreadingWSGIServer(ThreadingMixIn, WSGIServer):
        daemon_threads = True

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    app = create_app(config_path)
    log.info("CAPI server on http://%s:%s (platforms: %s)", host, port, ", ".join(capi_platforms(app.config)))
    make_server(host, port, app, server_class=ThreadingWSGIServer).serve_forever()
