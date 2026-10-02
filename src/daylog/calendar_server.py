"""Minimal HTTP server for the bot's two read-only views.

Telegram is the only interface that changes anything (CLAUDE.md: "No
interactive web frontend"). This serves two things at unguessable paths and
nothing else: the calendar feed (a .ics file for a calendar app to poll)
and the status page (one static HTML page, see dashboard.py) — no forms,
no scripts, no browsing.
"""

from __future__ import annotations

import logging
import os
import threading
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

from daylog import calendar_feed, daily, dashboard
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


def _make_handler(feed_path: str, status_path: str) -> type[BaseHTTPRequestHandler]:
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

    return Handler


def start() -> None:
    """Start the read-only server in a background thread, if configured.

    Opt-in: with no CALENDAR_FEED_SECRET set, this does nothing — merging
    the feature doesn't require immediate setup, and an unset secret must
    never fall back to a guessable or open path.
    """
    secret = os.environ.get("CALENDAR_FEED_SECRET")
    if not secret:
        logger.info("CALENDAR_FEED_SECRET not set — calendar feed server not started")
        return

    feed_path = f"/calendar/{secret}.ics"
    port = int(os.environ.get("PORT", "8080"))
    status_path = f"/status/{secret}"
    server = ThreadingHTTPServer(("0.0.0.0", port), _make_handler(feed_path, status_path))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    logger.info("read-only server on port %d: /calendar/<secret>.ics and /status/<secret>", port)
