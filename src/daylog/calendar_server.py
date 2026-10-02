"""Minimal HTTP server: two read-only views and one data inlet.

Telegram is the only interface that changes anything you'd call content
(CLAUDE.md: "No interactive web frontend"). At unguessable paths this serves
the calendar feed (a .ics file for a calendar app to poll) and the status
page (one static HTML page, see dashboard.py), and accepts one thing: the
phone's daily health numbers (see health.py), under its own secret — no
forms, no scripts, no browsing.
"""

from __future__ import annotations

import logging
import os
import threading
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

from daylog import calendar_feed, daily, dashboard, health
from daylog.tg import tz
from daylog.vault import Vault

logger = logging.getLogger(__name__)


def _vault() -> Vault:
    return Vault(Path(os.environ.get("VAULT_PATH", "../daylog-vault")))


def _build_feed_bytes() -> bytes:
    vault = _vault()
    journal_entries = [
        entry
        for entry_date in vault.list_journal_dates()
        if (entry := vault.read_journal_entry(entry_date)) is not None
    ]
    return calendar_feed.build_feed(vault.read_goals(), vault.read_itinerary(), journal_entries)


def _build_status_bytes() -> bytes:
    vault = _vault()
    return dashboard.render(vault, daily.logical_day(datetime.now(tz()))).encode("utf-8")


def _store_health(body: bytes) -> str:
    vault = _vault()
    day, metrics = health.parse(body, daily.logical_day(datetime.now(tz())))
    stored = health.merge(vault.read_yaml(health.FILE, {}), day, metrics)
    vault.write_yaml(health.FILE, stored, f"health: {day.isoformat()}")
    return f"saved {len(metrics)} metrics for {day.isoformat()}\n"


def _make_handler(
    feed_path: str | None, status_path: str | None, health_path: str | None
) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, format: str, *args: Any) -> None:
            logger.info("calendar server: " + format, *args)

        def do_GET(self) -> None:
            if self.path == feed_path:
                build, content_type = _build_feed_bytes, "text/calendar; charset=utf-8"
            elif self.path == status_path:
                build, content_type = _build_status_bytes, "text/html; charset=utf-8"
            else:
                self.send_response(404)
                self.end_headers()
                return

            try:
                body = build()
            except Exception:
                logger.exception("failed to build %s", self.path.split("/")[1])
                self.send_response(500)
                self.end_headers()
                return

            self.send_response(200)
            self.send_header("Content-Type", content_type)
            self.send_header("X-Robots-Tag", "noindex")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _reply(self, code: int, text: str) -> None:
            body = text.encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_POST(self) -> None:
            if health_path is None or self.path != health_path:
                self.send_response(404)
                self.end_headers()
                return
            try:
                length = int(self.headers.get("Content-Length") or 0)
            except ValueError:
                length = -1
            if not 0 < length <= health.MAX_BODY_BYTES:
                self._reply(413, "body missing or too large\n")
                return
            try:
                self._reply(200, _store_health(self.rfile.read(length)))
            except health.HealthError as exc:
                self._reply(400, f"{exc}\n")
            except Exception:
                logger.exception("failed to store health data")
                self._reply(500, "could not save\n")

    return Handler


def start() -> None:
    """Start the server in a background thread, if configured.

    Opt-in per feature: CALENDAR_FEED_SECRET enables the feed and the status
    page (STATUS_PAGE_SLUG moves the page to /status/<slug>),
    HEALTH_INGEST_SECRET enables the health inlet. With neither set
    this does nothing, and an unset secret never falls back to a guessable
    or open path.
    """
    view_secret = os.environ.get("CALENDAR_FEED_SECRET")
    health_secret = os.environ.get("HEALTH_INGEST_SECRET")
    # The status page lives at a path of the user's choosing when
    # STATUS_PAGE_SLUG is set (short and typeable, so weaker than the
    # secret); otherwise it shares the feed's secret.
    status_slug = os.environ.get("STATUS_PAGE_SLUG", "").strip("/ ") or view_secret
    if not view_secret and not health_secret and not status_slug:
        logger.info("no CALENDAR_FEED_SECRET or HEALTH_INGEST_SECRET — web server not started")
        return

    handler = _make_handler(
        f"/calendar/{view_secret}.ics" if view_secret else None,
        f"/status/{status_slug}" if status_slug else None,
        f"/health/{health_secret}" if health_secret else None,
    )
    port = int(os.environ.get("PORT", "8080"))
    server = ThreadingHTTPServer(("0.0.0.0", port), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    logger.info(
        "web server on port %d: views %s, health inlet %s",
        port,
        "on" if view_secret else "off",
        "on" if health_secret else "off",
    )
