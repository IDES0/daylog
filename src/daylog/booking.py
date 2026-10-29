"""Booking pages: a link someone can use to pick one of the user's free slots.

A page is a file in the vault, `booking/<slug>.yaml` (what it is, the days
and times on offer, the form's wording), served on its own domain at
`book.<domain>/<slug>`, with `?name=` filling in the guest's name. A guest's
booking is the only thing a stranger can write: it lands in `bookings.yaml`
as pending, the user gets one notification, and a card in the app asks them
to Accept or Decline. Only an accepted booking goes on the calendar feed, so
nothing a guest sends puts a hard date in the user's plans without a
Confirm. Nothing is ever sent back to the guest.

Pure logic plus the vault calls; calendar_server.py does the HTTP.
"""

from __future__ import annotations

import re
import threading
import time
import uuid
from datetime import date, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from daylog.vault import Vault

FOLDER = "booking"
FILE = "bookings.yaml"
SLUG_RE = re.compile(r"[a-z0-9][a-z0-9-]{0,39}")
TIME_RE = re.compile(r"([01]\d|2[0-3]):[0-5]\d")
MAX_BODY_BYTES = 4 * 1024
# A page stops taking bookings once this many wait for an answer, so a
# leaked link can't fill every slot or the vault.
MAX_PENDING_PER_PAGE = 10
_LIMITS = {"name": 80, "type": 80, "order": 120, "notes": 1000}

# One booking at a time, so two guests can't take the same slot.
_lock = threading.Lock()


class BookingError(ValueError):
    """Something the guest should be told, with the HTTP status to send."""

    def __init__(self, status: int, message: str) -> None:
        super().__init__(message)
        self.status = status
        self.message = message


class RateLimit:
    """At most `limit` bookings per client per `window` seconds, in memory."""

    def __init__(self, limit: int = 5, window: float = 3600) -> None:
        self._limit = limit
        self._window = window
        self._hits: dict[str, list[float]] = {}
        self._guard = threading.Lock()

    def allow(self, client: str, now: float | None = None) -> bool:
        at = time.time() if now is None else now
        with self._guard:
            hits = [t for t in self._hits.get(client, []) if at - t < self._window]
            if len(hits) >= self._limit:
                self._hits[client] = hits
                return False
            hits.append(at)
            self._hits[client] = hits
            return True


def page(v: Vault, slug: str) -> dict[str, Any] | None:
    """The page's spec, or None when there's no such page or it's closed."""
    if not SLUG_RE.fullmatch(slug):
        return None
    spec = v.read_yaml(f"{FOLDER}/{slug}.yaml", None)
    if not isinstance(spec, dict) or spec.get("open") is False:
        return None
    return dict(spec)


def preview(v: Vault, slug: str) -> bytes | None:
    """The page's link-preview image (`booking/<slug>.png`), if it has one."""
    if page(v, slug) is None:
        return None
    return v.read_bytes(f"{FOLDER}/{slug}.png")


def bookings(v: Vault) -> list[dict[str, Any]]:
    data = v.read_yaml(FILE, {}) or {}
    return [dict(b) for b in data.get("bookings") or [] if isinstance(b, dict)]


def label(hhmm: str) -> str:
    """'13:00' -> '1:00 PM'."""
    hour, minute = (int(x) for x in hhmm.split(":"))
    return f"{hour % 12 or 12}:{minute:02d} {'AM' if hour < 12 else 'PM'}"


def long_day(d: date) -> str:
    return f"{d:%A}, {d:%b} {d.day}"


def _taken(slug: str, current: list[dict[str, Any]]) -> set[tuple[str, str]]:
    return {
        (str(b["date"]), str(b["time"]))
        for b in current
        if b.get("page") == slug and b.get("status") in ("pending", "accepted")
    }


def _days(spec: dict[str, Any]) -> list[tuple[date, dict[str, Any]]]:
    out = []
    for day in spec.get("days") or []:
        try:
            out.append((date.fromisoformat(str(day["date"])), day))
        except (KeyError, TypeError, ValueError):
            continue
    return sorted(out, key=lambda pair: pair[0])


def view(
    slug: str, spec: dict[str, Any], current: list[dict[str, Any]], now: datetime
) -> dict[str, Any]:
    """What the page shows: its wording and each day's slots (open, taken or held)."""
    taken = _taken(slug, current)
    days = []
    for d, day in _days(spec):
        if d < now.date():
            continue
        held = {str(k): str(v) for k, v in (day.get("held") or {}).items()}
        slots = []
        for t in [str(x) for x in day.get("times") or []] + list(held):
            if not TIME_RE.fullmatch(t):
                continue
            start = datetime.combine(d, datetime.strptime(t, "%H:%M").time(), now.tzinfo)
            state = "held" if t in held else "taken" if (d.isoformat(), t) in taken else "open"
            if start <= now:
                state = "taken"
            slots.append({"time": t, "label": label(t), "state": state, "note": held.get(t, "")})
        slots.sort(key=lambda s: s["time"])
        days.append(
            {
                "date": d.isoformat(),
                "dow": f"{d:%a}",
                "short": f"{d:%b} {d.day}",
                "long": long_day(d),
                "slots": slots,
            }
        )
    title = str(spec.get("title") or "Book a time")
    return {
        "slug": slug,
        "kicker": str(spec.get("kicker") or ""),
        "title": title,
        "lede": str(spec.get("lede") or ""),
        "details": [[str(k), str(v)] for k, v in (spec.get("details") or {}).items()],
        "where": str(spec.get("where") or ""),
        "types": [str(t) for t in spec.get("types") or []],
        "order_label": str(spec.get("order_label") or ""),
        "notes_label": str(spec.get("notes_label") or "Anything I should know?"),
        "notes_hint": str(spec.get("notes_hint") or ""),
        "checks": [str(c) for c in spec.get("checks") or []],
        "fine": str(spec.get("fine") or ""),
        "ref_prefix": str(spec.get("ref_prefix") or "BOOK"),
        # What a link preview (iMessage, WhatsApp) shows.
        "share_title": str(spec.get("share_title") or re.sub(r"\*", "", title)),
        "share_text": str(spec.get("share_text") or spec.get("lede") or ""),
        "days": days,
    }


def _text(data: dict[str, Any], name: str) -> str:
    value = data.get(name)
    if value is None:
        return ""
    if not isinstance(value, str):
        raise BookingError(400, f"'{name}' must be text.")
    # One line for everything but the notes, which keep their line breaks.
    cleaned = value.strip() if name == "notes" else " ".join(value.split())
    return cleaned[: _LIMITS[name]]


def request(v: Vault, slug: str, data: dict[str, Any], now: datetime) -> dict[str, Any]:
    """Take a guest's booking: check the slot is on offer and free, then save it
    as pending. Returns the saved booking; raises BookingError otherwise."""
    with _lock:
        spec = page(v, slug)
        if spec is None:
            raise BookingError(404, "This booking page isn't open.")
        current = bookings(v)
        shown = view(slug, spec, current, now)
        day, at = str(data.get("date") or ""), str(data.get("time") or "")
        slot = next(
            (s for d in shown["days"] if d["date"] == day for s in d["slots"] if s["time"] == at),
            None,
        )
        if slot is None:
            raise BookingError(400, "Please pick one of the times shown.")
        if slot["state"] != "open":
            raise BookingError(409, "That time was just taken. Please pick another.")
        name = _text(data, "name")
        if not name:
            raise BookingError(400, "Please add your name.")
        kind = _text(data, "type")
        if shown["types"] and kind not in shown["types"]:
            kind = shown["types"][0]
        waiting = sum(1 for b in current if b.get("page") == slug and b.get("status") == "pending")
        if waiting >= MAX_PENDING_PER_PAGE:
            raise BookingError(429, "This page has too many bookings waiting. Try again later.")
        d = date.fromisoformat(day)
        record = {
            "id": "b_" + uuid.uuid4().hex[:10],
            "page": slug,
            "ref": f"{shown['ref_prefix']}-{d:%m%d}-{at.replace(':', '')}",
            "name": name,
            "date": day,
            "time": at,
            "minutes": int(spec.get("minutes") or 60),
            "where": shown["where"],
            "title": str(spec.get("event") or spec.get("title") or "Booking"),
            "type": kind,
            "order": _text(data, "order"),
            "notes": _text(data, "notes"),
            "status": "pending",
            "received": now.isoformat(timespec="seconds"),
        }
        v.write_yaml(FILE, {"bookings": [*current, record]}, f"booking: {name} for {day} {at}")
        return record


def when(b: dict[str, Any]) -> str:
    return f"{long_day(date.fromisoformat(str(b['date'])))}, {label(str(b['time']))}"


def notice(b: dict[str, Any]) -> tuple[str, str]:
    """The notification: who and when, then what they wrote."""
    extra = [str(b[k]) for k in ("type", "order") if b.get(k)]
    return f"📅 {b['name']} booked {when(b)}", " · ".join(extra) or str(b.get("title") or "")


def card_detail(b: dict[str, Any]) -> str:
    rows = [("When", when(b)), ("Where", b.get("where")), ("Type", b.get("type"))]
    rows += [("Order", b.get("order")), ("Notes", b.get("notes")), ("Ref", b.get("ref"))]
    return "\n".join(f"- {k}: {v}" for k, v in rows if v)


def pending(v: Vault) -> list[dict[str, Any]]:
    return [b for b in bookings(v) if b.get("status") == "pending"]


def answer(v: Vault, booking_id: str, accept: bool, on: date) -> dict[str, Any] | None:
    """Accept or decline a pending booking; None if it isn't pending any more."""
    with _lock:
        current = bookings(v)
        b = next((x for x in current if x.get("id") == booking_id), None)
        if b is None or b.get("status") != "pending":
            return None
        b["status"] = "accepted" if accept else "declined"
        b["answered"] = on.isoformat()
        verb = "accepted" if accept else "declined"
        v.write_yaml(FILE, {"bookings": current}, f"booking: {verb} {b['name']} {b['date']}")
        return b


def events(current: list[dict[str, Any]], zone: ZoneInfo) -> list[dict[str, Any]]:
    """Accepted bookings as timed calendar events (start/end in `zone`)."""
    out = []
    for b in current:
        if b.get("status") != "accepted":
            continue
        try:
            d = date.fromisoformat(str(b["date"]))
            t = datetime.strptime(str(b["time"]), "%H:%M").time()
        except (KeyError, ValueError):
            continue
        start = datetime.combine(d, t, zone)
        out.append(
            {
                "uid": f"booking-{b.get('id')}@daylog",
                "summary": f"{b.get('title') or 'Booking'}: {b.get('name')}",
                "start": start,
                "end": start + timedelta(minutes=int(b.get("minutes") or 60)),
                "location": str(b.get("where") or ""),
                "description": card_detail(b),
            }
        )
    return out
