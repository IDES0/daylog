"""Minimal HTTP server: the calendar feed, the status site and web app, one data inlet.

This serves the calendar feed at an unguessable path (a .ics file for a
calendar app to poll), the status site at / (see dashboard.py: the guest
view; once the password line has set a session, see web.py, the web app's
shell from webapp/static, falling back to the private view), the files that
make it installable as an app, the web app's JSON API under /api/ (see
webapp/api.py: session first, then the CSRF header on anything but GET),
and the phone's daily health numbers (see health.py), under its own secret.
Request bodies are never logged.
"""

from __future__ import annotations

import json
import logging
import os
import threading
from datetime import date, datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlsplit

from daylog import calendar_feed, daily, dashboard, health, web
from daylog.runtime import tz
from daylog.vault import Vault
from daylog.webapp import api as webapi

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


def _week_of(query: str) -> date | None:
    """The `week` query value (any ISO date in that week), or None if absent or bad."""
    value = (parse_qs(query).get("week") or [""])[0]
    try:
        return date.fromisoformat(value)
    except ValueError:
        return None


def _build_status_bytes(
    private: bool = False, login: bool = False, week_of: date | None = None
) -> bytes:
    vault = _vault()
    today = daily.logical_day(datetime.now(tz()))
    return dashboard.render(vault, today, private=private, login=login, week_of=week_of).encode(
        "utf-8"
    )


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
    app: webapi.Api | None = None,
) -> type[BaseHTTPRequestHandler]:
    # One guard per server, so failed attempts are counted across requests.
    guard = web.LoginGuard(password) if password and site else None
    # One API per server too: it holds the open cards and the web chat.
    api = (app or webapi.Api()) if site else None

    class Handler(BaseHTTPRequestHandler):
        web_api = api

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

        def _land(self, cookie: str | None = None) -> None:
            # A page, not a 303: see web.LANDING.
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            if cookie:
                self.send_header("Set-Cookie", cookie)
            self.send_header("Content-Length", str(len(web.LANDING)))
            self.end_headers()
            self.wfile.write(web.LANDING)

        def _not_found(self) -> None:
            self.send_response(404)
            self.end_headers()

        def _signed_in(self) -> bool:
            return web.valid_session(web.session_from(self.headers.get("Cookie")), password)

        def do_GET(self) -> None:
            url = urlsplit(self.path)
            path = url.path
            build: Any
            if feed_path and path == feed_path:
                build, content_type = _build_feed_bytes, "text/calendar; charset=utf-8"
            elif api and path.startswith("/api/"):
                self._api("GET")
                return
            elif site and path == "/" and self._signed_in():
                # Signed in: the web app's shell, or the private view without one.
                shell = webapi.static_file("index.html")
                if shell is not None:
                    self._send(200, webapi.STATIC_FILES["index.html"], shell)
                    return
                week = _week_of(url.query)
                build = lambda: _build_status_bytes(True, guard is not None, week)  # noqa: E731
                content_type = "text/html; charset=utf-8"
            elif site and path in ("/app.js", "/app.css"):
                asset = webapi.static_file(path[1:])
                if asset is None:
                    self._not_found()
                    return
                self._send(200, webapi.STATIC_FILES[path[1:]], asset, cache="no-cache")
                return
            elif site and path == "/":
                # One address: the guest view when signed out.
                build = lambda: _build_status_bytes(False, guard is not None)  # noqa: E731
                content_type = "text/html; charset=utf-8"
            elif site and path in _SHELL:
                kind, body = _SHELL[path]
                # The worker is always re-checked, so a fix to it reaches
                # an installed app on its next open.
                cache = "no-cache" if path == "/sw.js" else "public, max-age=86400"
                self._send(200, kind, body, cache=cache)
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
                self._land(web.clear_cookie())
            elif api and path.startswith("/api/"):
                self._api("POST")
            else:
                self._not_found()

        def _json(self, code: int, payload: dict[str, Any]) -> None:
            body = json.dumps(payload, ensure_ascii=False, default=str).encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Robots-Tag", "noindex")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _api(self, method: str) -> None:
            # No CORS headers, ever: another site's page can't read or send these.
            assert api is not None
            url = urlsplit(self.path)
            req = webapi.Request(
                method=method,
                path=url.path,
                query=parse_qs(url.query),
                headers={k.lower(): v for k, v in self.headers.items()},
                signed_in=self._signed_in(),
            )
            refused = api.precheck(req)
            if refused is not None:
                self._json(*refused)
                return
            if method == "POST":
                try:
                    length = int(self.headers.get("Content-Length") or 0)
                except ValueError:
                    self._json(400, {"error": "bad_length"})
                    return
                if length < 0 or length > webapi.body_limit(url.path):
                    self._json(413, {"error": "too_large", "message": "The body is too large."})
                    return
                req.body = self.rfile.read(length) if length else b""
            self._json(*api.handle(req))

        def _login(self) -> None:
            # Right password: a session, so / shows the private view. Anything
            # else (wrong, too many tries, no password configured): back to the
            # guest view, with nothing to say which it was.
            body = self._body(web.MAX_LOGIN_BODY)
            if guard and password and body and guard.attempt(web.password_from(body)):
                logger.info("status login")
                self._land(web.set_cookie(web.make_session(password)))
                return
            logger.warning("status login refused")
            self._land()

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


def start(app: webapi.Api | None = None) -> ThreadingHTTPServer | None:
    """Start the server in a background thread, if configured; returns it.

    `app` is the web app's API (its card store and chat), shared with the
    scheduler so a scheduled plan's card shows up in the app.

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
        logger.warning("no web feature configured — web server not started")
        return None

    handler = _make_handler(
        f"/calendar/{view_secret}.ics" if view_secret else None,
        f"/health/{health_secret}" if health_secret else None,
        site,
        password,
        app,
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
    return server
