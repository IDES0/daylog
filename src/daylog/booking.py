"""Booking pages: a link someone can use to pick one of the user's free slots.

A page is a file in the vault, `booking/<slug>.yaml` (what it is, the times
on offer, the form's wording), served on its own domain at
`book.<domain>/<slug>`; `book.<domain>/` itself serves `booking/index.yaml`,
the generic page. `?name=` (or the page's `guest`) fills in the guest's name.

Pages compose: `extends: <slug>` takes everything from another page and
overrides only what the file says, so one base (`template: true`, never
served itself) can hold the availability and many personal pages sit on top
with their own wording and `theme`. Availability is a `weekly` pattern over
the next `horizon` days, plus per-date `days` overrides and holds. A slot
taken on any page is taken on all of them: they share one person's time.
`once: true` makes a one-time link that closes when its booking is accepted.

A guest's booking is the only thing a stranger can write: it lands in
`bookings.yaml` as pending, the user gets one notification, and a card in
the app asks them to Accept or Decline. Only an accepted booking goes on the
calendar feed, so nothing a guest sends puts a hard date in the user's plans
without a Confirm. Nothing is ever sent back to the guest.

Pure logic plus the vault calls; calendar_server.py does the HTTP and
webapp/api.py the app's management screen.
"""

from __future__ import annotations

import re
import threading
import time
import uuid
from collections.abc import Mapping
from datetime import date, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from daylog.vault import Vault

FOLDER = "booking"
FILE = "bookings.yaml"
# `book.<domain>/` serves this page.
ROOT_SLUG = "index"
SLUG_RE = re.compile(r"[a-z0-9][a-z0-9-]{0,39}")
TIME_RE = re.compile(r"([01]\d|2[0-3]):[0-5]\d")
MAX_BODY_BYTES = 4 * 1024
# A page stops taking bookings once this many wait for an answer, so a
# leaked link can't fill every slot or the vault.
MAX_PENDING_PER_PAGE = 10
_LIMITS = {"name": 80, "type": 80, "order": 120, "notes": 1000}
DEFAULT_HORIZON = 14
MAX_HORIZON = 90
MAX_EXTENDS = 5

WEEKDAYS = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")
_DAY_GROUPS = {
    **{d: (i,) for i, d in enumerate(WEEKDAYS)},
    "weekdays": (0, 1, 2, 3, 4),
    "weekends": (5, 6),
    "daily": (0, 1, 2, 3, 4, 5, 6),
}

# Every key a page file may have, with what it holds: the chat tool and the
# app's editor validate against this, so a typo fails loudly instead of
# silently doing nothing.
TEXT_KEYS = {
    "kicker": 120,
    "title": 200,
    "lede": 1200,
    "where": 200,
    "event": 120,
    "ref_prefix": 12,
    "guest": 80,
    "order_label": 120,
    "notes_label": 200,
    "notes_hint": 200,
    "name_label": 80,
    "type_label": 80,
    "submit_label": 60,
    "stamp": 40,
    "done_text": 400,
    "checks_error": 200,
    "fine": 600,
    "share_title": 200,
    "share_text": 400,
}
KEYS = set(TEXT_KEYS) | {
    "extends",
    "template",
    "open",
    "once",
    "theme",
    "minutes",
    "details",
    "types",
    "checks",
    "weekly",
    "horizon",
    "from",
    "until",
    "days",
}
# Said about one page only, never taken from the page it extends.
_OWN_KEYS = {"extends", "template", "open", "once", "guest"}

# Preset looks: the page's colour and font tokens, light and dark. A page
# picks one by name; nothing else about its styling is configurable.
# fmt: off
_FIRM_LIGHT = {
    "bg": "#eef1ee", "card": "#ffffff", "ink": "#16231d", "muted": "#5d6b64",
    "line": "#d5ddd8", "accent": "#1f5c43", "accent-ink": "#ffffff", "hi": "#f4e27a",
    "dis": "#e7ebe8",
}
_FIRM_DARK = {
    "bg": "#101714", "card": "#18221d", "ink": "#e6ede9", "muted": "#97a69e",
    "line": "#2c3a33", "accent": "#6cc39b", "accent-ink": "#0e1a14", "hi": "#6b5f1c",
    "dis": "#1f2a24",
}
_NIGHT = {
    "bg": "#17131f", "card": "#221c2e", "ink": "#efe9f7", "muted": "#a59bb8",
    "line": "#3a3148", "accent": "#d4a5ff", "accent-ink": "#1a1024", "hi": "#5a3f73",
    "dis": "#2a2337",
}
THEMES: dict[str, dict[str, Any]] = {
    "firm": {
        "label": "Firm: green, an intake form",
        "light": _FIRM_LIGHT,
        "dark": _FIRM_DARK,
        "fonts": {},
    },
    "surf": {
        "label": "Surf: ocean blue and sand",
        "light": {
            "bg": "#eaf3f5", "card": "#ffffff", "ink": "#0f2a33", "muted": "#557079",
            "line": "#cfe0e4", "accent": "#0e6e85", "accent-ink": "#ffffff", "hi": "#ffe3a3",
            "dis": "#e1ecef",
        },
        "dark": {
            "bg": "#0b1a20", "card": "#12252c", "ink": "#e3f1f4", "muted": "#8fb0b8",
            "line": "#23404a", "accent": "#5cc8de", "accent-ink": "#06171c", "hi": "#6a5320",
            "dis": "#182e36",
        },
        "fonts": {"serif": "Fraunces", "sans": "DM Sans"},
    },
    "sunset": {
        "label": "Sunset: warm and playful",
        "light": {
            "bg": "#fdf1e9", "card": "#ffffff", "ink": "#2d1a14", "muted": "#7a5d52",
            "line": "#f0d6c8", "accent": "#d9532b", "accent-ink": "#ffffff", "hi": "#ffd46b",
            "dis": "#f6e4da",
        },
        "dark": {
            "bg": "#1d1210", "card": "#291a16", "ink": "#f6e7e0", "muted": "#bfa093",
            "line": "#47302a", "accent": "#ff8a5c", "accent-ink": "#2a120a", "hi": "#6e4b16",
            "dis": "#33231e",
        },
        "fonts": {"serif": "Young Serif", "sans": "Nunito"},
    },
    "alpine": {
        "label": "Alpine: slate and dawn orange",
        "light": {
            "bg": "#eef0f4", "card": "#ffffff", "ink": "#1a2030", "muted": "#5b6475",
            "line": "#d6dbe5", "accent": "#3c4f8a", "accent-ink": "#ffffff", "hi": "#ffcf8a",
            "dis": "#e4e7ee",
        },
        "dark": {
            "bg": "#10131b", "card": "#181d29", "ink": "#e5e8f0", "muted": "#959db0",
            "line": "#2b3242", "accent": "#8fa6ec", "accent-ink": "#0e1220", "hi": "#6b4f22",
            "dis": "#1e2431",
        },
        "fonts": {"sans": "Space Grotesk"},
    },
    "night": {
        "label": "Night: dark violet, for dinner or drinks",
        "light": _NIGHT,
        "dark": _NIGHT,
        "fonts": {"serif": "Cormorant Garamond", "sans": "Manrope"},
    },
    "paper": {
        "label": "Paper: cream and ink, like a letter",
        "light": {
            "bg": "#f6f2ea", "card": "#fffdf8", "ink": "#26211b", "muted": "#6f665b",
            "line": "#e3dccf", "accent": "#8a3b2a", "accent-ink": "#ffffff", "hi": "#f1df9c",
            "dis": "#ece6da",
        },
        "dark": {
            "bg": "#191714", "card": "#23201b", "ink": "#eee7dc", "muted": "#a69d90",
            "line": "#3a352d", "accent": "#e39a83", "accent-ink": "#241310", "hi": "#5c4d1e",
            "dis": "#2b2722",
        },
        "fonts": {"serif": "EB Garamond"},
    },
}
# fmt: on
DEFAULT_THEME = "firm"

# One booking at a time, so two guests can't take the same slot.
_lock = threading.Lock()


class BookingError(ValueError):
    """Something the guest (or the user editing a page) should be told, with
    the HTTP status to send."""

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


# -- reading pages ----------------------------------------------------------------


def _path(slug: str) -> str:
    return f"{FOLDER}/{slug}.yaml"


def raw(v: Vault, slug: str) -> dict[str, Any] | None:
    """The page file as written (its live ruamel mapping), or None."""
    if not SLUG_RE.fullmatch(slug):
        return None
    spec = v.read_yaml(_path(slug), None)
    return spec if isinstance(spec, dict) else None


def slugs(v: Vault) -> list[str]:
    out = []
    for name in v.list_docs(FOLDER, "*.yaml"):
        stem = name.rsplit("/", 1)[-1].removesuffix(".yaml")
        if SLUG_RE.fullmatch(stem):
            out.append(stem)
    return out


def _resolve(spec: Mapping[str, Any], lookup: Any) -> dict[str, Any]:
    """`spec` with everything it inherits filled in (child keys win)."""
    chain = [dict(spec)]
    seen: set[str] = set()
    parent = spec.get("extends")
    while parent and len(chain) <= MAX_EXTENDS:
        name = str(parent)
        if name in seen:
            break
        seen.add(name)
        base = lookup(name)
        if not isinstance(base, Mapping):
            break
        chain.append(dict(base))
        parent = base.get("extends")
    merged: dict[str, Any] = {}
    for layer in reversed(chain[1:]):
        merged.update({k: val for k, val in layer.items() if k not in _OWN_KEYS})
    merged.update(chain[0])
    return merged


def resolve(v: Vault, slug: str) -> dict[str, Any] | None:
    """The page with what it inherits, whether or not it's open; None if no file."""
    spec = raw(v, slug)
    return None if spec is None else _resolve(spec, lambda s: raw(v, s))


def _used(slug: str, current: list[dict[str, Any]]) -> bool:
    return any(b.get("page") == slug and b.get("status") == "accepted" for b in current)


def page(v: Vault, slug: str) -> dict[str, Any] | None:
    """The page's spec as served, or None when there's no such page, it's
    closed, it's a template, or it's a one-time link already used."""
    spec = resolve(v, slug)
    if spec is None or spec.get("open") is False or spec.get("template"):
        return None
    if spec.get("once") and _used(slug, bookings(v)):
        return None
    return spec


def preview(v: Vault, slug: str) -> bytes | None:
    """The page's link-preview image (`booking/<slug>.png`, or its base's)."""
    spec = page(v, slug)
    if spec is None:
        return None
    for name in [slug, *_ancestors(v, slug)]:
        image = v.read_bytes(f"{FOLDER}/{name}.png")
        if image is not None:
            return image
    return None


def _ancestors(v: Vault, slug: str) -> list[str]:
    out: list[str] = []
    spec = raw(v, slug)
    while spec and spec.get("extends") and len(out) < MAX_EXTENDS:
        name = str(spec["extends"])
        if name in out or name == slug:
            break
        out.append(name)
        spec = raw(v, name)
    return out


def bookings(v: Vault) -> list[dict[str, Any]]:
    data = v.read_yaml(FILE, {}) or {}
    return [dict(b) for b in data.get("bookings") or [] if isinstance(b, dict)]


# -- what a page shows ----------------------------------------------------------------


def label(hhmm: str) -> str:
    """'13:00' -> '1:00 PM'."""
    hour, minute = (int(x) for x in hhmm.split(":"))
    return f"{hour % 12 or 12}:{minute:02d} {'AM' if hour < 12 else 'PM'}"


def long_day(d: date) -> str:
    return f"{d:%A}, {d:%b} {d.day}"


def _start(b: Mapping[str, Any], zone: Any) -> datetime | None:
    try:
        d = date.fromisoformat(str(b["date"]))
        t = datetime.strptime(str(b["time"]), "%H:%M").time()
    except (KeyError, ValueError):
        return None
    return datetime.combine(d, t, zone)


def _busy(current: list[dict[str, Any]], zone: Any) -> list[tuple[datetime, datetime]]:
    """When the user is already booked, from any page: they share one person's time."""
    out = []
    for b in current:
        if b.get("status") not in ("pending", "accepted"):
            continue
        start = _start(b, zone)
        if start is not None:
            out.append((start, start + timedelta(minutes=int(b.get("minutes") or 60))))
    return out


def _times(value: Any) -> list[str]:
    return [str(t) for t in value or [] if TIME_RE.fullmatch(str(t))]


def _date_or(value: Any, default: date) -> date:
    try:
        return date.fromisoformat(str(value))
    except ValueError:
        return default


def _schedule(spec: Mapping[str, Any], today: date) -> list[tuple[date, list[str], dict[str, str]]]:
    """Each day on offer: the weekly pattern over the horizon, then each
    `days` entry replacing that date's times, adding holds or (`off`) removing it."""
    plan: dict[date, tuple[list[str], dict[str, str]]] = {}
    weekly = spec.get("weekly")
    if isinstance(weekly, Mapping) and weekly:
        horizon = max(1, min(MAX_HORIZON, int(spec.get("horizon") or DEFAULT_HORIZON)))
        first = max(today, _date_or(spec.get("from"), today))
        last = today + timedelta(days=horizon - 1)
        if spec.get("until"):
            last = min(last, _date_or(spec.get("until"), last))
        d = first
        while d <= last:
            offered: set[str] = set()
            for key, value in weekly.items():
                if d.weekday() in _DAY_GROUPS.get(str(key), ()):
                    offered.update(_times(value))
            if offered:
                plan[d] = (sorted(offered), {})
            d += timedelta(days=1)
    for day in spec.get("days") or []:
        if not isinstance(day, Mapping):
            continue
        try:
            d = date.fromisoformat(str(day["date"]))
        except (KeyError, ValueError):
            continue
        if day.get("off"):
            plan.pop(d, None)
            continue
        times, held = plan.get(d, ([], {}))
        if "times" in day:
            times = _times(day.get("times"))
        held = {**held, **{str(k): str(v) for k, v in (day.get("held") or {}).items()}}
        plan[d] = (times, held)
    return [(d, *plan[d]) for d in sorted(plan) if plan[d][0] or plan[d][1]]


def view(
    slug: str, spec: dict[str, Any], current: list[dict[str, Any]], now: datetime
) -> dict[str, Any]:
    """What the page shows: its wording and each day's slots (open, taken or held)."""
    zone = now.tzinfo
    busy = _busy(current, zone)
    minutes = int(spec.get("minutes") or 60)
    # A one-time link with a booking waiting has nothing left to offer.
    spent = bool(spec.get("once")) and any(
        b.get("page") == slug and b.get("status") in ("pending", "accepted") for b in current
    )
    days = []
    for d, times, held in _schedule(spec, now.date()):
        if d < now.date():
            continue
        slots = []
        for t in sorted(set(times) | set(held)):
            if not TIME_RE.fullmatch(t):
                continue
            start = datetime.combine(d, datetime.strptime(t, "%H:%M").time(), zone)
            end = start + timedelta(minutes=minutes)
            clash = any(s < end and start < e for s, e in busy)
            state = "held" if t in held else "taken" if clash or spent else "open"
            if start <= now:
                state = "taken"
            slots.append({"time": t, "label": label(t), "state": state, "note": held.get(t, "")})
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
    theme = str(spec.get("theme") or DEFAULT_THEME)
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
        "guest": str(spec.get("guest") or ""),
        # The form's wording; the defaults are the original intake-form voice.
        "name_label": str(spec.get("name_label") or "Full name"),
        "type_label": str(spec.get("type_label") or "Nature of engagement"),
        "submit_label": str(spec.get("submit_label") or "Confirm booking"),
        "stamp": str(spec.get("stamp") or "Booked"),
        "done_text": str(
            spec.get("done_text")
            or "That's it, nothing else to send. Alex has it and will confirm."
        ),
        "checks_error": str(
            spec.get("checks_error")
            or "All acknowledgments are required to proceed. Legal insisted."
        ),
        "theme": theme if theme in THEMES else DEFAULT_THEME,
        # What a link preview (iMessage, WhatsApp) shows.
        "share_title": str(spec.get("share_title") or re.sub(r"\*", "", title)),
        "share_text": str(spec.get("share_text") or spec.get("lede") or ""),
        "days": days,
    }


def theme_css(name: str) -> tuple[str, str]:
    """(a <style> body overriding the page's tokens, a Google Fonts css2 URL or '')."""
    theme = THEMES.get(name) or THEMES[DEFAULT_THEME]

    def tokens(values: Mapping[str, str]) -> str:
        return "".join(f"--{k}:{v};" for k, v in values.items())

    fonts = theme["fonts"]
    stacks = {
        "serif": "Georgia,serif",
        "sans": "system-ui,sans-serif",
        "mono": "ui-monospace,monospace",
    }
    faces = "".join(f'--{k}:"{fam}",{stacks[k]};' for k, fam in fonts.items())
    dark = tokens(theme["dark"]) + "color-scheme:dark;"
    css = (
        f":root{{{tokens(theme['light'])}{faces}}}"
        f'@media (prefers-color-scheme:dark){{:root:not([data-theme="light"]){{{dark}}}}}'
        f':root[data-theme="dark"]{{{dark}}}'
    )
    if theme["light"] is theme["dark"]:
        css = f":root{{{dark}{faces}}}"
    families = [
        "family="
        + fam.replace(" ", "+")
        + (":ital,wght@0,400;0,600;1,400" if k == "serif" else ":wght@400;500;600;700")
        for k, fam in fonts.items()
    ]
    url = (
        "https://fonts.googleapis.com/css2?" + "&".join(families) + "&display=swap"
        if families
        else ""
    )
    return css, url


# -- a guest's booking ----------------------------------------------------------------


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
    rows += [("Page", b.get("page"))]
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


def cancel(v: Vault, booking_id: str, on: date) -> dict[str, Any] | None:
    """Cancel a pending or accepted booking (the user tapped Cancel and confirmed):
    it leaves the calendar feed and its slot opens again. None if neither."""
    with _lock:
        current = bookings(v)
        b = next((x for x in current if x.get("id") == booking_id), None)
        if b is None or b.get("status") not in ("pending", "accepted"):
            return None
        b["status"] = "cancelled"
        b["answered"] = on.isoformat()
        v.write_yaml(FILE, {"bookings": current}, f"booking: cancelled {b['name']} {b['date']}")
        return b


def events(current: list[dict[str, Any]], zone: ZoneInfo) -> list[dict[str, Any]]:
    """Accepted bookings as timed calendar events (start/end in `zone`)."""
    out = []
    for b in current:
        if b.get("status") != "accepted":
            continue
        start = _start(b, zone)
        if start is None:
            continue
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


# -- managing pages (the app's Booking screen and the chat's proposals) -------------


def check(v: Vault, slug: str, spec: Any) -> None:
    """Raise BookingError(400) naming what's wrong with a page file, else return."""
    problems: list[str] = []
    if not SLUG_RE.fullmatch(slug):
        problems.append("the link name must be lower-case letters, digits and dashes")
    if not isinstance(spec, Mapping):
        raise BookingError(400, "A page must be a set of fields (key: value).")
    unknown = sorted(str(k) for k in spec if k not in KEYS)
    if unknown:
        problems.append(
            f"unknown field(s) {', '.join(unknown)}; allowed: {', '.join(sorted(KEYS))}"
        )
    for key, limit in TEXT_KEYS.items():
        value = spec.get(key)
        if value is not None and (not isinstance(value, str) or len(value) > limit):
            problems.append(f"{key} must be text of at most {limit} characters")
    for key in ("template", "open", "once"):
        if spec.get(key) is not None and not isinstance(spec.get(key), bool):
            problems.append(f"{key} must be true or false")
    theme = spec.get("theme")
    if theme is not None and theme not in THEMES:
        problems.append(f"theme must be one of {', '.join(THEMES)}")
    for key, low, high in (("minutes", 5, 600), ("horizon", 1, MAX_HORIZON)):
        value = spec.get(key)
        if value is not None and (
            isinstance(value, bool) or not isinstance(value, int) or not low <= value <= high
        ):
            problems.append(f"{key} must be a whole number from {low} to {high}")
    for key in ("from", "until"):
        if spec.get(key) is not None:
            try:
                date.fromisoformat(str(spec[key]))
            except ValueError:
                problems.append(f"{key} must be a date (YYYY-MM-DD)")
    details = spec.get("details")
    if details is not None and not isinstance(details, Mapping):
        problems.append("details must be label: text pairs")
    for key in ("types", "checks"):
        value = spec.get(key)
        if value is not None and (
            not isinstance(value, list) or not all(isinstance(x, str) for x in value)
        ):
            problems.append(f"{key} must be a list of text")
    weekly = spec.get("weekly")
    if weekly is not None:
        if not isinstance(weekly, Mapping):
            problems.append("weekly must map days (mon…sun, weekdays, weekends, daily) to times")
        else:
            for key, value in weekly.items():
                if str(key) not in _DAY_GROUPS:
                    problems.append(
                        f"weekly: {key!r} isn't a day (mon…sun, weekdays, weekends, daily)"
                    )
                elif not isinstance(value, list) or len(_times(value)) != len(value):
                    problems.append(f'weekly: {key} must be a list of times like "13:00"')
    days = spec.get("days")
    if days is not None:
        if not isinstance(days, list):
            problems.append("days must be a list")
        else:
            for day in days:
                problems += _check_day(day)
    parent = spec.get("extends")
    if parent is not None:
        problems += _check_parent(v, slug, str(parent))
    if problems:
        raise BookingError(400, "; ".join(problems) + ".")


def _check_day(day: Any) -> list[str]:
    if not isinstance(day, Mapping):
        return ["each of days must be a set of fields with a date"]
    try:
        d = date.fromisoformat(str(day.get("date")))
    except ValueError:
        return [f"days: {day.get('date')!r} isn't a date (YYYY-MM-DD)"]
    out = []
    times = day.get("times")
    if times is not None and (not isinstance(times, list) or len(_times(times)) != len(times)):
        out.append(f'days {d}: times must be a list like ["11:00", "13:00"]')
    held = day.get("held")
    if held is not None and (not isinstance(held, Mapping) or len(_times(list(held))) != len(held)):
        out.append(f'days {d}: held must map times like "13:00" to a note')
    unknown = set(map(str, day)) - {"date", "times", "held", "off"}
    if unknown:
        out.append(f"days {d}: unknown field(s) {', '.join(sorted(unknown))}")
    return out


def _check_parent(v: Vault, slug: str, parent: str) -> list[str]:
    if parent == slug:
        return ["a page can't extend itself"]
    if raw(v, parent) is None:
        return [f"extends {parent!r}, but there's no such page"]
    if slug in _ancestors(v, parent) or len(_ancestors(v, parent)) >= MAX_EXTENDS:
        return [f"extending {parent!r} would make a loop or a chain too long"]
    return []


def children(v: Vault, slug: str) -> list[str]:
    """The pages that extend `slug` directly."""
    return [s for s in slugs(v) if s != slug and str((raw(v, s) or {}).get("extends")) == slug]


def save(v: Vault, slug: str, spec: Mapping[str, Any], *, create: bool = False) -> str:
    """Write a whole page file after checking it. `create` refuses to overwrite."""
    exists = raw(v, slug) is not None
    if create and exists:
        raise BookingError(409, f"There's already a page called {slug}.")
    check(v, slug, spec)
    verb = "edit" if exists else "new page"
    v.write_yaml(_path(slug), spec, f"booking: {verb} {slug}")
    return f"{'Saved' if exists else 'Made'} the booking page {slug}"


def update(v: Vault, slug: str, changes: Mapping[str, Any], *, create: bool = False) -> str:
    """Merge field changes into a page (a None value removes the field) and save it."""
    current = raw(v, slug)
    if current is None and not create:
        raise BookingError(404, f"There's no booking page called {slug}.")
    if current is not None and create:
        raise BookingError(409, f"There's already a page called {slug}.")
    spec: Any = current if current is not None else {}
    for key, value in changes.items():
        if value is None:
            spec.pop(key, None)
        else:
            spec[key] = value
    return save(v, slug, spec)


def preview_update(
    v: Vault, slug: str, changes: Mapping[str, Any], *, create: bool = False
) -> dict[str, Any]:
    """What `update` would write, checked but not saved (the chat's dry run)."""
    current = raw(v, slug)
    if current is None and not create:
        raise BookingError(404, f"There's no booking page called {slug}.")
    if current is not None and create:
        raise BookingError(409, f"There's already a page called {slug}.")
    spec = dict(current or {})
    for key, value in changes.items():
        if value is None:
            spec.pop(key, None)
        else:
            spec[key] = value
    check(v, slug, spec)
    return spec


def set_open(v: Vault, slug: str, is_open: bool) -> str:
    update(v, slug, {"open": None if is_open else False})
    return f"{'Opened' if is_open else 'Closed'} {slug}"


def delete(v: Vault, slug: str) -> str:
    """Remove a page (and its preview image); its bookings stay in bookings.yaml."""
    if raw(v, slug) is None:
        raise BookingError(404, f"There's no booking page called {slug}.")
    kids = children(v, slug)
    if kids:
        raise BookingError(
            409,
            f"{', '.join(kids)} extend{'s' if len(kids) == 1 else ''} {slug}; "
            "change or delete them first.",
        )
    names = [_path(slug)]
    if v.read_bytes(f"{FOLDER}/{slug}.png") is not None:
        names.append(f"{FOLDER}/{slug}.png")
    v.delete_files(names, f"booking: delete {slug}")
    return f"Deleted the booking page {slug}"


def link_path(slug: str) -> str:
    return "/" if slug == ROOT_SLUG else f"/{slug}"


def overview(v: Vault, now: datetime) -> list[dict[str, Any]]:
    """Every page for the app: its state, link, open slots and bookings."""
    current = bookings(v)
    out = []
    for slug in slugs(v):
        spec = resolve(v, slug) or {}
        own = raw(v, slug) or {}
        if spec.get("template"):
            status = "template"
        elif spec.get("open") is False:
            status = "closed"
        elif spec.get("once") and _used(slug, current):
            status = "used"
        else:
            status = "open"
        shown = view(slug, spec, current, now)
        free = [(d, s) for d in shown["days"] for s in d["slots"] if s["state"] == "open"]
        mine = sorted(
            (b for b in current if b.get("page") == slug),
            key=lambda b: (str(b.get("date")), str(b.get("time"))),
            reverse=True,
        )
        out.append(
            {
                "slug": slug,
                "path": link_path(slug),
                "title": re.sub(r"\*", "", str(spec.get("title") or "Book a time")),
                "status": status,
                "extends": str(own.get("extends") or ""),
                "children": children(v, slug),
                "guest": str(own.get("guest") or ""),
                "theme": shown["theme"],
                "once": bool(own.get("once")),
                "minutes": int(spec.get("minutes") or 60),
                "open_slots": 0 if status != "open" else len(free),
                "next_open": f"{free[0][0]['long']}, {free[0][1]['label']}"
                if free and status == "open"
                else "",
                "bookings": [
                    {
                        "id": str(b.get("id")),
                        "name": str(b.get("name") or ""),
                        "when": when(b),
                        "date": str(b.get("date")),
                        "status": str(b.get("status") or ""),
                        "type": str(b.get("type") or ""),
                        "notes": str(b.get("notes") or ""),
                    }
                    for b in mine[:20]
                ],
            }
        )
    # Templates last; otherwise by name.
    return sorted(out, key=lambda p: (p["status"] == "template", p["slug"]))


def describe(slug: str, spec: Mapping[str, Any]) -> str:
    """A page file as short lines, for a confirm card."""
    rows = []
    for key in sorted(spec, key=lambda k: (k not in ("extends", "guest", "theme", "title"), k)):
        value = spec[key]
        if isinstance(value, Mapping):
            text = "; ".join(f"{k}: {v}" for k, v in value.items())
        elif isinstance(value, list):
            text = "; ".join(str(x) for x in value)
        else:
            text = str(value)
        rows.append(f"- {key}: {text[:300]}")
    return f"Link: {link_path(slug)}\n" + "\n".join(rows)
