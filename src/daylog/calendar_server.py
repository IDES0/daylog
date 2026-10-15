"""Minimal HTTP server: the calendar feed, the status site, one data inlet.

Telegram is the only interface that changes anything you'd call content
(CLAUDE.md). This serves the calendar feed at an unguessable path (a .ics
file for a calendar app to poll), the status site at / (see dashboard.py: the
guest view, or the private view once the password line has set a session,
see web.py, plus the files that make it installable as an app), and
accepts one thing: the phone's daily health numbers (see health.py), under
its own secret. The password line and sign-out are the only forms.
"""

from __future__ import annotations

import logging
import os
import threading
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from daylog import calendar_feed, daily, dashboard, health, web
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


def _build_status_bytes(private: bool = False, login: bool = False) -> bytes:
    vault = _vault()
    today = daily.logical_day(datetime.now(tz()))
    return dashboard.render(vault, today, private=private, login=login).encode("utf-8")


# Unchanging files of the app shell: path -> (content type, body).
_SHELL: dict[str, tuple[str, bytes]] = {
    "/manifest.webmanifest": ("application/manifest+json", web.manifest()),
    "/sw.js": ("text/javascript; charset=utf-8", web.SERVICE_WORKER),
    "/icon.svg": ("image/svg+xml", web.icon_svg()),
    "/robots.txt": ("text/plain; charset=utf-8", web.ROBOTS),
}
_ICONS = {"/icon-180.png": 180, "/icon-192.png": 192, "/icon-512.png": 512}


def _store_health(body: bytes) -> str:
    vault = _vault()
    day, metrics = health.parse(body, daily.logical_day(datetime.now(tz())))
    stored = health.merge(vault.read_yaml(health.FILE, {}), day, metrics)
    vault.write_yaml(health.FILE, stored, f"health: {day.isoformat()}")
    left_out = health.skipped(body, metrics)
    note = f"; skipped (no number): {', '.join(left_out)}" if left_out else ""
    logger.info("health saved for %s: %s%s", day, ", ".join(metrics), note)
    return f"saved {', '.join(metrics)} for {day.isoformat()}{note}\n"


def _make_handler(
    feed_path: str | None,
    health_path: str | None,
    site: bool = False,
    password: str | None = None,
) -> type[BaseHTTPRequestHandler]:
    # One guard per server, so failed attempts are counted across requests.
    guard = web.LoginGuard(password) if password and site else None

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, format: str, *args: Any) -> None:
            logger.info("calendar server: " + format, *args)

        def _send(self, code: int, content_type: str, body: bytes, cache: str = "no-store") -> None:
            self.send_response(code)
            self.send_header("Content-Type", content_type)
            self.send_header("X-Robots-Tag", "noindex")
            self.send_header("Cache-Control", cache)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _redirect(self, to: str, cookie: str | None = None) -> None:
            self.send_response(303)
            self.send_header("Location", to)
            self.send_header("Cache-Control", "no-store")
            if cookie:
                self.send_header("Set-Cookie", cookie)
            self.send_header("Content-Length", "0")
            self.end_headers()

        def _not_found(self) -> None:
            self.send_response(404)
            self.end_headers()

        def _signed_in(self) -> bool:
            return web.valid_session(web.session_from(self.headers.get("Cookie")), password)

        def do_GET(self) -> None:
            path = urlsplit(self.path).path
            build: Any
            if feed_path and path == feed_path:
                build, content_type = _build_feed_bytes, "text/calendar; charset=utf-8"
            elif site and path == "/":
                # One address: the private view when signed in, else the guest one.
                private = self._signed_in()
                build = lambda: _build_status_bytes(private, guard is not None)  # noqa: E731
                content_type = "text/html; charset=utf-8"
            elif site and path in _SHELL:
                kind, body = _SHELL[path]
                self._send(200, kind, body, cache="public, max-age=86400")
                return
            elif site and path in _ICONS:
                body = web.icon_png(_ICONS[path])
                self._send(200, "image/png", body, cache="public, max-age=86400")
                return
            else:
                self._not_found()
                return

            try:
                body = build()
            except Exception:
                logger.exception("failed to build %s", path)
                self.send_response(500)
                self.end_headers()
                return
            self._send(200, content_type, body)

        def _reply(self, code: int, text: str) -> None:
            self._send(code, "text/plain; charset=utf-8", text.encode("utf-8"))

        def _body(self, limit: int) -> bytes | None:
            try:
                length = int(self.headers.get("Content-Length") or 0)
            except ValueError:
                return None
            if not 0 < length <= limit:
                return None
            return self.rfile.read(length)

        def do_POST(self) -> None:
            path = urlsplit(self.path).path
            if health_path and path == health_path:
                self._store_health()
            elif site and path == "/login":
                self._login()
            elif site and path == "/logout":
                self._redirect("/", web.clear_cookie())
            else:
                self._not_found()

        def _login(self) -> None:
            # Right password: a session, so / shows the private view. Anything
            # else (wrong, too many tries, no password configured): back to the
            # guest view, with nothing to say which it was.
            body = self._body(web.MAX_LOGIN_BODY)
            if guard and password and body and guard.attempt(web.password_from(body)):
                logger.info("status login")
                self._redirect("/", web.set_cookie(web.make_session(password)))
                return
            logger.warning("status login refused")
            self._redirect("/")

        def _store_health(self) -> None:
            body = self._body(health.MAX_BODY_BYTES)
            if body is None:
                self._reply(413, "body missing or too large\n")
                return
            try:
                self._reply(200, _store_health(body))
            except health.HealthError as exc:
                logger.warning("health post rejected: %s", exc)
                self._reply(400, f"{exc}\n")
            except Exception:
                logger.exception("failed to store health data")
                self._reply(500, "could not save\n")

    return Handler


def start() -> None:
    """Start the server in a background thread, if configured.

    Opt-in per feature: CALENDAR_FEED_SECRET enables the feed,
    STATUS_PASSWORD enables the status site at / with its password line
    (STATUS_SITE=1 serves the guest view alone, without a password),
    HEALTH_INGEST_SECRET enables the health inlet. With none set this does
    nothing, and an unset secret never falls back to a guessable or open path.
    """
    view_secret = os.environ.get("CALENDAR_FEED_SECRET")
    health_secret = os.environ.get("HEALTH_INGEST_SECRET")
    password = os.environ.get("STATUS_PASSWORD") or None
    site = bool(password) or os.environ.get("STATUS_SITE", "").strip().lower() in (
        "1",
        "true",
        "on",
    )
    if not view_secret and not health_secret and not site:
        logger.info("no web feature configured — web server not started")
        return

    handler = _make_handler(
        f"/calendar/{view_secret}.ics" if view_secret else None,
        f"/health/{health_secret}" if health_secret else None,
        site,
        password,
    )
    port = int(os.environ.get("PORT", "8080"))
    server = ThreadingHTTPServer(("0.0.0.0", port), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    logger.info(
        "web server on port %d: feed %s, status site %s, private view %s, health inlet %s",
        port,
        "on" if view_secret else "off",
        "on" if site else "off",
        "on" if password else "off",
        "on" if health_secret else "off",
    )
