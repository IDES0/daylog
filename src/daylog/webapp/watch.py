"""The watch inlet: log a note or ask the assistant from an Apple Watch Shortcut.

watchOS has no browser, so the web app can't run there, and a Shortcut can't
hold the session cookie. Instead each device gets its own token, made in the
app (You → Watch & Shortcuts, `POST /api/devices`), shown once, kept in the
vault only as a SHA-256 hash (`devices.yaml`) and revocable there:

    POST /watch/log   Authorization: Bearer <token>   {"text": "..."}
    POST /watch/ask   Authorization: Bearer <token>   {"text": "..."}
    POST /watch/brief Authorization: Bearer <token>   (no body)

Outside /api/* on purpose: the session-and-CSRF rule stays whole for the
app, and a token can do exactly two things. `log` runs the same pipeline as
the app's Log tab (journal_service, in the background, spooled across a
restart) and answers at once; when the note is in, one notification says
what was logged. `ask` is one chat turn in the same conversation as the
Chat tab: if the answer is ready within WAIT_SECONDS it comes back as
plain text for the watch to show or speak, else the watch hears "thinking"
and the answer arrives as one notification. Anything inferred (a goal
change, a new place) still waits as a card in the app: the watch can't
confirm, so nothing it says is applied without a tap. `brief` makes the
morning brief now, for a Shortcut run when the user wakes (after it has
synced last night's sleep): once a day, announced by the brief's usual
notification; the clock's 7am brief then skips the day. Asked again once
it's made, it answers with the brief itself, as plain text to show or speak.

Replies are plain text, since that's what a Shortcut shows best. Bodies,
tokens and transcripts are never logged.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import re
import secrets
import threading
import time
from collections import deque
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import date
from typing import TYPE_CHECKING, Any

from daylog import push

if TYPE_CHECKING:
    from daylog.vault import Vault
    from daylog.webapp.api import Api

logger = logging.getLogger(__name__)

FILE = "devices.yaml"
TOKEN_PREFIX = "dl_"
MAX_BODY_BYTES = 16 * 1024
MAX_TEXT = 8000
MAX_DEVICES = 10
WAIT_SECONDS = 20.0
# Per device: this many requests per window. Chat costs money; a leaked
# token shouldn't be able to run up the bill faster than this.
RATE_LIMIT = 30
RATE_WINDOW_S = 600.0
# All devices: after this many bad tokens in the window, every request fails.
MAX_MISSES = 10
MISS_WINDOW_S = 900.0
PUSH_BODY_CHARS = 900
PATHS = ("/watch/log", "/watch/ask", "/watch/brief")

Reply = tuple[int, str]


# -- devices and tokens -----------------------------------------------------------------


def _hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def devices(v: Vault) -> list[dict[str, Any]]:
    data = v.read_yaml(FILE, {}) or {}
    return [dict(d) for d in data.get("devices") or [] if isinstance(d, Mapping)]


def public(device: Mapping[str, Any]) -> dict[str, Any]:
    """What the app may see of a device: never the hash."""
    return {k: device.get(k) for k in ("id", "name", "created", "revoked")}


def add(v: Vault, name: str, today: date) -> tuple[dict[str, Any], str]:
    """A new device and its token (the only time the token exists in clear)."""
    name = re.sub(r"[^\w .'-]", "", name).strip()[:40] or "Apple Watch"
    current = devices(v)
    if sum(1 for d in current if not d.get("revoked")) >= MAX_DEVICES:
        raise ValueError(f"at most {MAX_DEVICES} devices; revoke one first")
    token = TOKEN_PREFIX + secrets.token_urlsafe(24)
    device = {
        "id": secrets.token_hex(4),
        "name": name,
        "hash": _hash(token),
        "created": today.isoformat(),
    }
    v.write_yaml(FILE, {"devices": [*current, device]}, f"devices: add {name}")
    return device, token


def revoke(v: Vault, device_id: str, today: date) -> bool:
    current = devices(v)
    found = False
    for d in current:
        if d.get("id") == device_id and not d.get("revoked"):
            d["revoked"] = today.isoformat()
            found = True
    if found:
        v.write_yaml(FILE, {"devices": current}, f"devices: revoke {device_id}")
    return found


def find(v: Vault, token: str) -> dict[str, Any] | None:
    """The live device this token belongs to, compared in constant time."""
    if not token.startswith(TOKEN_PREFIX):
        return None
    wanted = _hash(token)
    match = None
    for d in devices(v):
        if hmac.compare_digest(str(d.get("hash") or ""), wanted) and not d.get("revoked"):
            match = d
    return match


def bearer(headers: Mapping[str, str]) -> str:
    value = headers.get("authorization") or ""
    if value.lower().startswith("bearer "):
        return value[7:].strip()
    return (headers.get("x-daylog-token") or "").strip()


# -- what the watch says and hears ------------------------------------------------------


def said(body: bytes, content_type: str) -> str:
    """The note's text from a JSON {"text": ...} body or a plain-text one."""
    raw = body.decode("utf-8", "replace").strip()
    if "json" in content_type or raw.startswith("{"):
        try:
            data = json.loads(raw)
        except ValueError:
            return ""
        value = data.get("text") if isinstance(data, dict) else None
        return str(value).strip()[:MAX_TEXT] if value else ""
    return raw[:MAX_TEXT]


_DRAFT = re.compile(r"```draft[^\n]*\n(.*?)(?:```|\Z)", re.S)


def _draft_said(match: re.Match[str]) -> str:
    """A draft block read out: who it's for and the text; sending happens in the app."""
    lines = match.group(1).strip().splitlines()
    head = {}
    while lines and (
        m := re.match(r"\s*(to|number|phone|email|subject)\s*:\s*(.*)", lines[0], re.I)
    ):
        head[m.group(1).lower()] = m.group(2).strip()
        lines.pop(0)
    who = f" for {head['to']}" if head.get("to") else ""
    body = " ".join(line.strip() for line in lines if line.strip())
    return f"Draft{who}: {body} (open daylog to send it)"


def spoken(markdown: str) -> str:
    """A chat reply as plain text a watch can show or read out: no markdown marks."""
    text = _DRAFT.sub(_draft_said, markdown)
    text = re.sub(r"\[([^\]]+)\]\((?:https?://)?[^)]+\)", r"\1", text)
    lines = []
    for raw in text.splitlines():
        line = re.sub(r"^\s*(#+|[-*•]|\d+[.)])\s+", "", raw)
        line = re.sub(r"[*_`]+", "", line).strip()
        if line:
            lines.append(line)
    return "\n".join(lines)


def _clip(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def _waiting(cards: list[Any]) -> str:
    n = len(cards)
    return f" ({n} to confirm in the app)" if n else ""


# -- the inlet ------------------------------------------------------------------------------


@dataclass
class Request:
    path: str
    headers: Mapping[str, str]  # lower-case names
    body: bytes
    links: str = "/book"  # where booking links live (webapp.api.book_base)


class Watch:
    def __init__(
        self,
        api: Api,
        spawn: Callable[[Callable[[], None]], None] | None = None,
        notify: Callable[[Vault, str, str, str], int] = push.send,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.api = api
        self._spawn = spawn or _thread
        self._notify = notify
        self._clock = clock
        self._lock = threading.Lock()
        self._misses: deque[float] = deque()
        self._recent: dict[str, deque[float]] = {}
        self._briefing = False

    def _too_many_misses(self, now: float) -> bool:
        while self._misses and self._misses[0] <= now - MISS_WINDOW_S:
            self._misses.popleft()
        return len(self._misses) >= MAX_MISSES

    def _over_rate(self, device_id: str, now: float) -> bool:
        seen = self._recent.setdefault(device_id, deque())
        while seen and seen[0] <= now - RATE_WINDOW_S:
            seen.popleft()
        if len(seen) >= RATE_LIMIT:
            return True
        seen.append(now)
        return False

    def handle(self, req: Request) -> Reply:
        if req.path not in PATHS:
            return 404, "not found"
        now = self._clock()
        v = self.api._vault()
        with self._lock:
            if self._too_many_misses(now):
                logger.warning("watch inlet: refusing, too many bad tokens")
                return 429, "Too many tries. Wait a few minutes."
            device = find(v, bearer(req.headers))
            if device is None:
                self._misses.append(now)
                logger.warning("watch inlet: bad or revoked token")
                return 401, "This device isn't set up. Add it in daylog: You → Watch & Shortcuts."
            if self._over_rate(str(device["id"]), now):
                return 429, "That's a lot at once. Try again in a few minutes."
        if len(req.body) > MAX_BODY_BYTES:
            return 413, "That's too long for one note."
        if req.path == "/watch/brief":
            logger.info("watch inlet: brief from %s", device.get("name"))
            return self._brief(v)
        text = said(req.body, req.headers.get("content-type", ""))
        if not text:
            return 400, "I didn't catch anything to send."
        logger.info("watch inlet: %s from %s", req.path.rsplit("/", 1)[-1], device.get("name"))
        if req.path == "/watch/log":
            return self._log(v, text)
        return self._ask(v, text, req.links)

    def _log(self, v: Vault, text: str) -> Reply:
        api = self.api
        from daylog.webapp.api import ApiError
        from daylog.webapp.jobs import key_for

        def work() -> tuple[int, dict[str, Any]]:
            try:
                status, body = api._log(text, None)
            except ApiError as exc:
                status, body = exc.reply()
            if status == 200:
                summary = push.lock_screen(str(body.get("summary") or ""), PUSH_BODY_CHARS)
                self._notify(v, "📝 Logged", summary + _waiting(body.get("cards") or []), "/#/home")
            else:
                self._notify(
                    v,
                    "Couldn't log that",
                    "Your note is kept: open the Log tab to retry.",
                    "/#/log",
                )
            return status, body

        key = key_for(None, "log", text.encode(), "watch")
        api.jobs.submit(key, "log", work, {"text": text, "date": None})
        return 202, "Got it. Logging now; you'll get a notification when it's in."

    def _brief(self, v: Vault) -> Reply:
        from daylog import scheduler, services

        now = self.api._now()
        last = services.last_brief(v)
        if last is not None and last.date == now.date():
            # Already made (by the 7:00 run or on waking): read it back, so a
            # "my brief" shortcut can show or speak it on the watch.
            return 200, spoken(last.text)
        with self._lock:
            if self._briefing:
                return 202, "Already making it."
            self._briefing = True

        def work() -> None:
            try:
                scheduler.morning_brief(self.api, v, now)
            except Exception:
                logger.exception("watch brief failed")
            finally:
                with self._lock:
                    self._briefing = False

        self._spawn(work)
        return 202, "Good morning. Making your brief; it'll arrive as a notification in a minute."

    def _ask(self, v: Vault, text: str, links: str) -> Reply:
        done = threading.Event()
        state: dict[str, Any] = {"waiting": True}
        lock = threading.Lock()

        def work() -> None:
            try:
                out = self.api.ask(text, links)
                answer = spoken(str(out.get("reply") or "")) + _waiting(out.get("cards") or [])
            except Exception:
                logger.exception("watch ask failed")
                answer = "Something went wrong answering that. Try the Chat tab."
            with lock:
                state["answer"] = answer
                late = not state["waiting"]
            done.set()
            if late:
                self._notify(v, "daylog", _clip(answer, PUSH_BODY_CHARS), "/#/chat")

        self._spawn(work)
        done.wait(WAIT_SECONDS)
        with lock:
            state["waiting"] = False
            answer = state.get("answer")
        if answer is not None:
            return 200, answer
        return 202, "Thinking. The answer will come as a notification."


def _thread(fn: Callable[[], None]) -> None:
    threading.Thread(target=fn, daemon=True).start()
