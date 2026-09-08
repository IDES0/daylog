"""Minimal HTTP server exposing the read-only calendar feed.

The bot has no other HTTP surface — Telegram is the only interface
(CLAUDE.md's non-negotiables: "No web frontend"). This isn't one either:
it serves exactly one machine-readable resource (a .ics file) at an
unguessable path, for a calendar app to poll, and nothing else — no
pages, no forms, no browsing.
"""

from __future__ import annotations

import logging
import os
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

from daylog import calendar_feed
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


def _make_handler(feed_path: str) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, format: str, *args: Any) -> None:
            logger.info("calendar server: " + format, *args)

        def do_GET(self) -> None:
            if self.path != feed_path:
                self.send_response(404)
                self.end_headers()
                return

            try:
                body = _build_feed_bytes()
            except Exception:
                logger.exception("failed to build calendar feed")
                self.send_response(500)
                self.end_headers()
                return

            self.send_response(200)
            self.send_header("Content-Type", "text/calendar; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    return Handler


def start() -> None:
    """Start the feed server in a background thread, if configured.

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
    server = ThreadingHTTPServer(("0.0.0.0", port), _make_handler(feed_path))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    logger.info("calendar feed server listening on port %d at /calendar/<secret>.ics", port)
