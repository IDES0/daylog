"""Read-only iCalendar feed built from goals, itinerary, and past journal activity.

Pure logic — no filesystem access (vault.py owns that; the caller passes
in already-loaded data). One VEVENT per goal/itinerary deadline or
target, plus one all-day VEVENT per day with a journal entry, plus a timed
VEVENT per accepted booking and, for recent days, one per block of the
week calendar (see `block_events`): what was told, the watch's workouts,
sleep and long stretches of screen time. This is a
view into daylog's data for a calendar app to render: nothing here
writes back to the vault, and a subscribed calendar is read-only by
construction — editing an event in Google/Apple Calendar has no effect
on daylog.
"""

from __future__ import annotations

import logging
import re
from datetime import date, datetime, timedelta, tzinfo
from typing import Any

from icalendar import Calendar, Event

from daylog.health import wall
from daylog.timeline import Block
from daylog.vault import JournalEntry

logger = logging.getLogger(__name__)

PRODID = "-//daylog//daylog//EN"

_HEADING_RE = re.compile(r"^### \d{2}:\d{2}$")


def _add_all_day_event(
    cal: Calendar,
    uid: str,
    summary: str,
    start: date,
    end: date | None = None,
    description: str | None = None,
    status: str | None = None,
) -> None:
    event = Event()
    event.add("uid", uid)
    event.add("summary", summary)
    event.add("dtstart", start)
    # DTEND for an all-day event is exclusive per RFC 5545 — a single day
    # ends the day after it starts, not on the same day.
    event.add("dtend", (end or start) + timedelta(days=1))
    if description:
        event.add("description", description)
    if status:
        event.add("status", status)
    cal.add_component(event)


# itinerary/goal `status` -> the RFC 5545 VEVENT STATUS calendar apps
# actually render differently (e.g. Apple Calendar shows TENTATIVE events
# distinctly) — a loose "maybe sometime in November" candidate plan
# shouldn't look identical to a firm, decided one.
_ICS_STATUS = {
    "candidate": "TENTATIVE",
    "planned": "CONFIRMED",
    "current": "CONFIRMED",
    "done": "CONFIRMED",
}


def _goal_events(cal: Calendar, goals_data: list[dict[str, Any]]) -> None:
    for goal in goals_data:
        if goal.get("status") == "dropped" or not goal.get("id"):
            continue
        title = goal.get("title", goal["id"])
        if goal.get("type") == "hard" and goal.get("deadline"):
            _add_all_day_event(
                cal, f"goal-deadline-{goal['id']}@daylog", f"Deadline: {title}", goal["deadline"]
            )
        elif goal.get("target_window") and goal["target_window"][-1]:
            _add_all_day_event(
                cal,
                f"goal-target-{goal['id']}@daylog",
                f"Target: {title}",
                goal["target_window"][-1],
            )


def _itinerary_events(cal: Calendar, itinerary_data: list[dict[str, Any]]) -> None:
    for entry in itinerary_data:
        if entry.get("status") == "dropped" or not entry.get("id"):
            continue
        place = entry.get("place", entry["id"])
        if entry.get("type") == "hard" and entry.get("deadline"):
            _add_all_day_event(
                cal, f"itin-deadline-{entry['id']}@daylog", f"Deadline: {place}", entry["deadline"]
            )
        elif entry.get("target_window") and entry["target_window"][0]:
            window = entry["target_window"]
            end = window[-1] if len(window) > 1 and window[-1] else None
            status = entry.get("status", "candidate")
            _add_all_day_event(
                cal,
                f"itin-target-{entry['id']}@daylog",
                f"{place} ({status})",
                window[0],
                end,
                status=_ICS_STATUS.get(status),
            )


def _timed_events(cal: Calendar, timed: list[dict[str, Any]]) -> None:
    """Events with a start and end time (accepted bookings, see booking.events)."""
    for item in timed:
        event = Event()
        event.add("uid", item["uid"])
        event.add("summary", item["summary"])
        event.add("dtstart", item["start"])
        event.add("dtend", item["end"])
        if item.get("location"):
            event.add("location", item["location"])
        if item.get("description"):
            event.add("description", item["description"])
        cal.add_component(event)


# A screen-time stretch shorter than this stays off the subscribed calendar.
FEED_SCREEN_MIN_HOURS = 0.5
_TIME_MARK = {"measured": "", "said": "", "estimated": "~", "guessed": "≈"}
_TIME_NOTE = {
    "measured": "Measured by your watch.",
    "said": "At the time you said.",
    "estimated": "Time estimated from what you said.",
    "guessed": "No time said: placed after what you told before it.",
}


def _block_title(b: Block) -> str:
    if b.layer == "sleep":
        asleep = b.facts.get("asleep_h")
        return f"Sleep {asleep:.1f} h" if isinstance(asleep, int | float) else "Sleep"
    if b.layer == "screen":
        device = "iPhone" if b.source == "iphone" else "Mac"
        minutes = b.facts.get("minutes") or 0
        return f"{device}: {b.detail} ({int(minutes) // 60} h {int(minutes) % 60:02d})"
    name = b.kind.replace("_", " ") + (f" {b.session}" if b.session else "")
    return f"{_TIME_MARK.get(b.time, '')}{name}" + (" ⌚" if b.time == "measured" else "")


def _block_description(b: Block) -> str:
    if b.layer == "screen":
        apps = b.facts.get("apps") or {}
        return "Screen time: " + ", ".join(f"{a} {m} min" for a, m in apps.items())
    if b.layer == "sleep":
        stages = [
            f"{k} {b.facts[f'{k}_h']:.1f} h"
            for k in ("deep", "rem", "core")
            if b.facts.get(f"{k}_h")
        ]
        return "Measured by your watch." + (f" {', '.join(stages)}." if stages else "")
    facts = [f"{k.replace('_', ' ')} {v}" for k, v in b.facts.items() if k != "kind"]
    lines = [b.detail, _TIME_NOTE.get(b.time, ""), ", ".join(facts)]
    return "\n".join(line for line in lines if line)


def block_events(day: date, blocks: list[Block], tz: tzinfo) -> list[dict[str, Any]]:
    """The day's calendar blocks as timed events for the feed (hours past 24 run
    into the next morning). Short screen stretches are left out, and so is
    sleep: the calendar splits a night at the day cutoff, the feed doesn't
    (see `sleep_events`)."""
    midnight = datetime.combine(day, datetime.min.time())
    out = []
    for i, b in enumerate(sorted(blocks, key=lambda b: (b.start, b.layer))):
        if b.layer == "sleep" or (b.layer == "screen" and b.end - b.start < FEED_SCREEN_MIN_HOURS):
            continue
        out.append(
            {
                "uid": f"block-{day.isoformat()}-{b.layer}-{i}@daylog",
                "summary": _block_title(b),
                "start": (midnight + timedelta(hours=b.start)).replace(tzinfo=tz),
                "end": (midnight + timedelta(hours=b.end)).replace(tzinfo=tz),
                "description": _block_description(b),
            }
        )
    return out


def sleep_events(nights: list[dict[str, Any]], tz: tzinfo) -> list[dict[str, Any]]:
    """Each measured night once, at its real start and end."""
    out = []
    for night in nights:
        start, end = wall(night.get("start")), wall(night.get("end"))
        if not start or not end or end <= start:
            continue
        b = Block("sleep", "", 0, 0, "measured", layer="sleep", facts=dict(night))
        out.append(
            {
                "uid": f"sleep-{start:%Y%m%dT%H%M}@daylog",
                "summary": _block_title(b),
                "start": start.replace(tzinfo=tz),
                "end": end.replace(tzinfo=tz),
                "description": _block_description(b),
            }
        )
    return out


def _journal_summary_line(entry: JournalEntry) -> str:
    """Strip the '### HH:MM' per-note headings, leaving flowing prose."""
    return " ".join(
        line.strip()
        for line in entry.summary.splitlines()
        if line.strip() and not _HEADING_RE.match(line.strip())
    )


def _journal_title(entry: JournalEntry) -> str:
    activities = entry.frontmatter.get("activities") or []
    types: list[str] = []
    if isinstance(activities, list):
        for activity in activities:
            activity_type = activity.get("type") if isinstance(activity, dict) else None
            if activity_type and activity_type not in types:
                types.append(activity_type)
    else:
        # Legacy/malformed data (e.g. extraction once wrote a JSON string
        # here instead of a list — see extract.py's _validate_shape) must
        # never crash the whole feed over one bad day. Fall back to the
        # generic title below rather than raise.
        logger.warning(
            "journal entry %s has non-list activities (%s) — using generic title",
            entry.date,
            type(activities).__name__,
        )
    base = ", ".join(types) if types else "journal entry"
    location = entry.frontmatter.get("location")
    return f"{base} — {location}" if location else base


def _journal_events(cal: Calendar, journal_entries: list[JournalEntry]) -> None:
    for entry in journal_entries:
        description = _journal_summary_line(entry)
        _add_all_day_event(
            cal,
            f"journal-{entry.date.isoformat()}@daylog",
            _journal_title(entry),
            entry.date,
            description=description or None,
        )


def build_feed(
    goals_data: list[dict[str, Any]],
    itinerary_data: list[dict[str, Any]],
    journal_entries: list[JournalEntry],
    timed: list[dict[str, Any]] | None = None,
) -> bytes:
    cal = Calendar()
    cal.add("prodid", PRODID)
    cal.add("version", "2.0")
    cal.add("calscale", "GREGORIAN")
    cal.add("x-wr-calname", "daylog")

    _goal_events(cal, goals_data)
    _itinerary_events(cal, itinerary_data)
    _journal_events(cal, journal_entries)
    _timed_events(cal, timed or [])

    ical_bytes: bytes = cal.to_ical()
    return ical_bytes
