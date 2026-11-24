"""What's booked, read from email by the research routines, never by the app.

The app has no mail access. The nightly evening routine (docs/routines/
requests.md, on the user's plan, Gmail read-only) reads booking
confirmations for yesterday through the next two weeks and writes one file
per day, `research/times/<date>.yaml`:

    - id: waco-surf-48213          # stable per booking (source + reference)
      kind: surf                   # an activity word the journal uses, or
                                   # flight, tour, meal, appointment, event, stay
      title: Waco Surf session
      place: Waco Surf
      at: "16:00"                  # local time at the booking's place
      until: "17:00"               # optional
      status: booked               # booked | changed | cancelled
      source: "email: Waco Surf booking confirmation"

This module only reads and checks those files. The week calendar draws a
booking as a `booked` block, or draws the told activity it matches at the
booked time when the journal didn't say one (a said time still wins, and a
watch workout wins over both); nothing is written back to the journal. The
brief gets today's bookings. Cancelled ones are dropped.
"""

from __future__ import annotations

import re
from datetime import date, timedelta
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from daylog.vault import Vault

DIR = "research/times"
DEFAULT_HOURS = 1.0
_CLOCK = re.compile(r"^(\d{1,2}):(\d{2})$")
_KIND = re.compile(r"[^a-z0-9_]+")


def _clock(value: Any) -> str | None:
    match = _CLOCK.match(str(value or "").strip())
    if not match or int(match.group(1)) > 23 or int(match.group(2)) > 59:
        return None
    return f"{int(match.group(1)):02d}:{match.group(2)}"


def _after(at: str, hours: float) -> str:
    h, m = int(at[:2]), int(at[3:])
    total = min(h * 60 + m + round(hours * 60), 23 * 60 + 59)
    return f"{total // 60:02d}:{total % 60:02d}"


def clean(raw: Any) -> list[dict[str, Any]]:
    """A day file's bookings, checked: a start time required, cancelled dropped."""
    items = raw.get("bookings") if isinstance(raw, dict) else raw
    out = []
    for b in items if isinstance(items, list) else []:
        if not isinstance(b, dict) or str(b.get("status") or "booked").lower() == "cancelled":
            continue
        at = _clock(b.get("at"))
        if at is None:
            continue
        until = _clock(b.get("until"))
        if until is None or until <= at:
            until = _after(at, DEFAULT_HOURS)
        kind = _KIND.sub("_", str(b.get("kind") or "booking").strip().lower()).strip("_")
        out.append(
            {
                "id": str(b.get("id") or f"{kind}-{at}")[:80],
                "kind": kind or "booking",
                "title": str(b.get("title") or b.get("place") or kind)[:120],
                "place": str(b.get("place") or "")[:120],
                "at": at,
                "until": until,
                "source": str(b.get("source") or "email")[:160],
            }
        )
    return sorted(out, key=lambda b: b["at"])


def day_file(day: date) -> str:
    return f"{DIR}/{day.isoformat()}.yaml"


def read(v: Vault, start: date, end: date) -> dict[date, list[dict[str, Any]]]:
    """Each day's bookings, start..end inclusive (days without any are absent)."""
    out: dict[date, list[dict[str, Any]]] = {}
    day = start
    while day <= end:
        found = clean(v.read_yaml(day_file(day), None))
        if found:
            out[day] = found
        day += timedelta(days=1)
    return out


def as_records(day: date, bookings: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Bookings shaped like measured records (start/end stamps), for matching
    against told activities the way watch workouts are (see timeline.py)."""
    return [
        {
            **b,
            "start": f"{day.isoformat()} {b['at']}:00",
            "end": f"{day.isoformat()} {b['until']}:00",
            "_booked": True,
        }
        for b in bookings
    ]


def brief_text(v: Vault, today: date) -> str:
    """Today's and tomorrow's bookings, one line each; '' when there are none."""
    found = read(v, today, today + timedelta(days=1))
    lines = []
    for day, items in sorted(found.items()):
        label = "Today" if day == today else "Tomorrow"
        for b in items:
            place = f" at {b['place']}" if b["place"] and b["place"] not in b["title"] else ""
            lines.append(f"- {label} {b['at']}–{b['until']}: {b['title']}{place} ({b['source']})")
    return "\n".join(lines)
