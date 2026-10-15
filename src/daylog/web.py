"""The status site's door and its app shell.

The status site has two views (see dashboard.py): a guest page anyone at the
address can read, and a private page behind one password line. A wrong
password just lands back on the guest page, so the line never says whether
there is anything behind it. The right one sets a signed session cookie.

Also here: the files that let a phone add the page to its home screen as an
app (manifest, service worker, icons). The icons are drawn in code, so
nothing is read from disk (vault.py stays the only module that touches it).

Pure logic: calendar_server.py does the HTTP.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import struct
import threading
import time
import zlib
from collections import deque
from functools import lru_cache
from http.cookies import CookieError, SimpleCookie
from urllib.parse import parse_qs

SESSION_COOKIE = "daylog_session"
SESSION_DAYS = 30
# Failed attempts allowed across everyone in the window before every login
# fails (right password included) until the window passes. Global rather
# than per address: behind Railway's proxy the address is only a header.
MAX_FAILURES = 10
FAILURE_WINDOW_S = 15 * 60
MAX_LOGIN_BODY = 1024

NAVY = (0x1F, 0x3A, 0x5F)
ORANGE = (0xFB, 0x92, 0x3C)
WHITE = (0xFF, 0xFF, 0xFF)
PAPER = "#fbfaf7"


def _key(password: str) -> bytes:
    # Keyed on the password, so changing it signs everyone out.
    return hashlib.sha256(b"daylog-session:" + password.encode("utf-8")).digest()


def _sign(password: str, expires: int) -> str:
    return hmac.new(_key(password), str(expires).encode(), hashlib.sha256).hexdigest()


def make_session(password: str, now: float | None = None) -> str:
    expires = int(now if now is not None else time.time()) + SESSION_DAYS * 86400
    return f"{expires}.{_sign(password, expires)}"


def valid_session(token: str | None, password: str | None, now: float | None = None) -> bool:
    if not token or not password:
        return False
    expires_text, _, sig = token.partition(".")
    if not expires_text.isdigit():
        return False
    expires = int(expires_text)
    if expires < (now if now is not None else time.time()):
        return False
    return hmac.compare_digest(sig, _sign(password, expires))


def session_from(cookie_header: str | None) -> str | None:
    if not cookie_header:
        return None
    try:
        jar = SimpleCookie(cookie_header)
    except CookieError:
        return None
    morsel = jar.get(SESSION_COOKIE)
    return morsel.value if morsel else None


def set_cookie(token: str) -> str:
    return (
        f"{SESSION_COOKIE}={token}; Max-Age={SESSION_DAYS * 86400}; Path=/; "
        "HttpOnly; Secure; SameSite=Lax"
    )


def clear_cookie() -> str:
    return f"{SESSION_COOKIE}=; Max-Age=0; Path=/; HttpOnly; Secure; SameSite=Lax"


def password_from(body: bytes) -> str:
    """The `password` field of a urlencoded form body; empty if absent."""
    fields = parse_qs(body.decode("utf-8", errors="replace"), max_num_fields=4)
    return (fields.get("password") or [""])[0]


class LoginGuard:
    """Checks the password, and stops answering after too many misses."""

    def __init__(
        self, password: str, max_failures: int = MAX_FAILURES, window_s: float = FAILURE_WINDOW_S
    ) -> None:
        self._password = password.encode("utf-8")
        self._max = max_failures
        self._window = window_s
        self._failures: deque[float] = deque()
        self._lock = threading.Lock()

    def attempt(self, given: str, now: float | None = None) -> bool:
        now = now if now is not None else time.time()
        with self._lock:
            while self._failures and self._failures[0] <= now - self._window:
                self._failures.popleft()
            if len(self._failures) >= self._max:
                return False
            if hmac.compare_digest(given.encode("utf-8"), self._password):
                return True
            self._failures.append(now)
            return False


# -- app shell -------------------------------------------------------------


def manifest() -> bytes:
    return json.dumps(
        {
            "name": "daylog",
            "short_name": "daylog",
            "start_url": "/",
            "scope": "/",
            "display": "standalone",
            "background_color": PAPER,
            "theme_color": "#{:02x}{:02x}{:02x}".format(*NAVY),
            "icons": [
                {"src": "/icon-192.png", "sizes": "192x192", "type": "image/png"},
                {"src": "/icon-512.png", "sizes": "512x512", "type": "image/png"},
                {"src": "/icon.svg", "sizes": "any", "type": "image/svg+xml"},
            ],
        },
        indent=2,
    ).encode("utf-8")


# What the password line and sign-out answer with: a plain page that carries
# the cookie and moves on to /. Not a redirect: an iPhone home-screen app
# dropped the cookie from a 303, so the right password just showed the guest
# view again. Every outcome gets the same page, so it says nothing about
# whether the password was right. replace() keeps the POST out of history.
LANDING = (
    b'<!doctype html><meta charset="utf-8">'
    b'<meta http-equiv="refresh" content="0;url=/">'
    b'<script>location.replace("/")</script>'
)

# Nothing is cached: both pages carry live (and one private) data. The worker
# only exists so an installed app shows a plain note instead of the browser's
# error page when there's no connection. It leaves form posts alone (the
# password line, sign-out), so their cookies go straight to the browser.
SERVICE_WORKER = b"""self.addEventListener("install", () => self.skipWaiting());
self.addEventListener("activate", e => e.waitUntil(self.clients.claim()));
self.addEventListener("fetch", e => {
  if (e.request.mode !== "navigate" || e.request.method !== "GET") return;
  e.respondWith(fetch(e.request).catch(() => new Response(
    "<!doctype html><meta name=viewport content='width=device-width'>" +
    "<body style='font:16px system-ui;padding:24px;background:#fbfaf7;color:#1b1d21'>" +
    "Offline. daylog needs a connection.</body>",
    { headers: { "Content-Type": "text/html; charset=utf-8" } })));
});
"""

ROBOTS = b"User-agent: *\nDisallow: /\n"


def icon_svg() -> bytes:
    """A pin on navy: the map's 'you are here' mark."""
    return (
        b'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 64 64">'
        b'<rect width="64" height="64" rx="14" fill="#1f3a5f"/>'
        b'<circle cx="32" cy="32" r="13" fill="#fb923c" stroke="#fff" stroke-width="4"/>'
        b"</svg>"
    )


def _blend(a: tuple[int, int, int], b: tuple[int, int, int], t: float) -> tuple[int, int, int]:
    return (
        round(a[0] + (b[0] - a[0]) * t),
        round(a[1] + (b[1] - a[1]) * t),
        round(a[2] + (b[2] - a[2]) * t),
    )


@lru_cache(maxsize=4)
def icon_png(size: int) -> bytes:
    """The same mark as icon_svg, full-bleed (the phone rounds the corners)."""
    centre = (size - 1) / 2
    inner, outer = size * 13 / 64, size * 17 / 64
    rows = []
    for y in range(size):
        row = bytearray(b"\x00")  # filter: none
        for x in range(size):
            d = ((x - centre) ** 2 + (y - centre) ** 2) ** 0.5
            # Coverage of the ring edge and the dot edge, one pixel of softening.
            ring = min(1.0, max(0.0, outer - d + 0.5))
            dot = min(1.0, max(0.0, inner - d + 0.5))
            row += bytes(_blend(_blend(NAVY, WHITE, ring), ORANGE, dot))
        rows.append(bytes(row))

    def chunk(kind: bytes, data: bytes) -> bytes:
        body = kind + data
        return struct.pack(">I", len(data)) + body + struct.pack(">I", zlib.crc32(body))

    header = struct.pack(">IIBBBBB", size, size, 8, 2, 0, 0, 0)
    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", header)
        + chunk(b"IDAT", zlib.compress(b"".join(rows), 9))
        + chunk(b"IEND", b"")
    )
