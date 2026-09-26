"""SQLite persistence: orders, browser beacons and per-platform send jobs.

One job per (order, platform) so each platform retries independently and a
Shopify webhook retry never sends the same order twice.
"""

from __future__ import annotations

import json
import sqlite3
import threading
import time
from typing import Any

SCHEMA = """
CREATE TABLE IF NOT EXISTS orders (
    order_id TEXT PRIMARY KEY,
    checkout_token TEXT,
    payload TEXT NOT NULL,
    received_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS beacons (
    order_id TEXT PRIMARY KEY,
    checkout_token TEXT,
    data TEXT NOT NULL,
    received_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS jobs (
    order_id TEXT NOT NULL,
    platform TEXT NOT NULL,
    due_at REAL NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending',
    attempts INTEGER NOT NULL DEFAULT 0,
    detail TEXT,
    updated_at REAL NOT NULL,
    PRIMARY KEY (order_id, platform)
);
CREATE INDEX IF NOT EXISTS jobs_due ON jobs (status, due_at);
"""


class Store:
    def __init__(self, path: str):
        self._db = sqlite3.connect(path, check_same_thread=False, isolation_level=None)
        self._db.row_factory = sqlite3.Row
        self._db.execute("PRAGMA journal_mode=WAL")
        self._db.executescript(SCHEMA)
        self._lock = threading.Lock()

    def _exec(self, sql: str, params: tuple = ()) -> sqlite3.Cursor:
        with self._lock:
            return self._db.execute(sql, params)

    # -- writes from the web server ---------------------------------------
    def add_order(self, order_id: str, checkout_token: str | None, payload: dict[str, Any], jobs: dict[str, float]) -> bool:
        """Store the order and schedule {platform: due_at}. False if already seen."""
        now = time.time()
        with self._lock:
            cursor = self._db.execute(
                "INSERT OR IGNORE INTO orders VALUES (?, ?, ?, ?)",
                (order_id, checkout_token, json.dumps(payload), now),
            )
            if cursor.rowcount == 0:
                return False
            beacon = self._db.execute("SELECT 1 FROM beacons WHERE order_id = ?", (order_id,)).fetchone()
            for platform, due_at in jobs.items():
                # Browser identifiers already here: no need to wait for them.
                if beacon and platform != "google_ads":
                    due_at = now
                self._db.execute(
                    "INSERT OR IGNORE INTO jobs (order_id, platform, due_at, updated_at) VALUES (?, ?, ?, ?)",
                    (order_id, platform, due_at, now),
                )
            return True

    def add_beacon(self, order_id: str, checkout_token: str | None, data: dict[str, Any]) -> None:
        now = time.time()
        with self._lock:
            self._db.execute(
                "INSERT OR REPLACE INTO beacons VALUES (?, ?, ?, ?)",
                (order_id, checkout_token, json.dumps(data), now),
            )
            self._db.execute(
                "UPDATE jobs SET due_at = MIN(due_at, ?) WHERE order_id = ? AND status = 'pending' AND platform != 'google_ads'",
                (now, order_id),
            )

    # -- worker -----------------------------------------------------------
    def due_jobs(self, now: float | None = None, limit: int = 50) -> list[sqlite3.Row]:
        return self._exec(
            "SELECT * FROM jobs WHERE status = 'pending' AND due_at <= ? ORDER BY due_at LIMIT ?",
            (now or time.time(), limit),
        ).fetchall()

    def claim(self, order_id: str, platform: str) -> bool:
        cursor = self._exec(
            "UPDATE jobs SET status = 'sending', updated_at = ? WHERE order_id = ? AND platform = ? AND status = 'pending'",
            (time.time(), order_id, platform),
        )
        return cursor.rowcount == 1

    def finish(self, order_id: str, platform: str, status: str, detail: str = "") -> None:
        self._exec(
            "UPDATE jobs SET status = ?, detail = ?, attempts = attempts + 1, updated_at = ? WHERE order_id = ? AND platform = ?",
            (status, detail[:1000], time.time(), order_id, platform),
        )

    def retry_later(self, order_id: str, platform: str, due_at: float, detail: str) -> None:
        self._exec(
            "UPDATE jobs SET status = 'pending', due_at = ?, detail = ?, attempts = attempts + 1, updated_at = ? "
            "WHERE order_id = ? AND platform = ?",
            (due_at, detail[:1000], time.time(), order_id, platform),
        )

    def order_and_beacon(self, order_id: str) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
        order = self._exec("SELECT payload, checkout_token FROM orders WHERE order_id = ?", (order_id,)).fetchone()
        beacon = self._exec("SELECT data, checkout_token FROM beacons WHERE order_id = ?", (order_id,)).fetchone()
        if not order:
            return None, None
        # A beacon only counts if it came from the same checkout (the
        # /collect endpoint is public, so don't trust an order ID alone).
        if beacon and order["checkout_token"] and beacon["checkout_token"] != order["checkout_token"]:
            beacon = None
        return json.loads(order["payload"]), (json.loads(beacon["data"]) if beacon else None)

    def job(self, order_id: str, platform: str) -> sqlite3.Row | None:
        return self._exec("SELECT * FROM jobs WHERE order_id = ? AND platform = ?", (order_id, platform)).fetchone()

    def stats(self) -> dict[str, dict[str, int]]:
        rows = self._exec("SELECT platform, status, COUNT(*) AS n FROM jobs GROUP BY platform, status").fetchall()
        result: dict[str, dict[str, int]] = {}
        for row in rows:
            result.setdefault(row["platform"], {})[row["status"]] = row["n"]
        return result

    def purge(self, older_than_days: float = 7) -> None:
        """Delete customer data once it's no longer needed for sending."""
        cutoff = time.time() - older_than_days * 86400
        with self._lock:
            self._db.execute("DELETE FROM orders WHERE received_at < ?", (cutoff,))
            self._db.execute("DELETE FROM beacons WHERE received_at < ?", (cutoff,))
            self._db.execute("DELETE FROM jobs WHERE updated_at < ? AND status != 'pending'", (cutoff,))
