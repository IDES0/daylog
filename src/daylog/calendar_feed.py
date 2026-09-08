"""Read-only iCalendar feed built from goals, itinerary, and past journal activity.

Pure logic — no filesystem access (vault.py owns that; the caller passes
in already-loaded data). One VEVENT per goal/itinerary deadline or
target, plus one all-day VEVENT per day with a journal entry. This is a
view into daylog's data for a calendar app to render: nothing here
writes back to the vault, and a subscribed calendar is read-only by
construction — editing an event in Google/Apple Calendar has no effect
on daylog.
"""

from __future__ import annotations

import re
from datetime import date, timedelta
from typing import Any

from icalendar import Calendar, Event

from daylog.vault import JournalEntry

PRODID = "-//daylog//daylog//EN"

_HEADING_RE = re.compile(r"^### \d{2}:\d{2}$")


def _add_all_day_event(
    cal: Calendar,
    uid: str,
    summary: str,
    start: date,
    end: date | None = None,
    description: str | None = None,
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
    cal.add_component(event)


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
            _add_all_day_event(
                cal,
                f"itin-target-{entry['id']}@daylog",
                f"{place} ({entry.get('status', 'candidate')})",
                window[0],
                end,
            )


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
    for activity in activities:
        activity_type = activity.get("type")
        if activity_type and activity_type not in types:
            types.append(activity_type)
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
) -> bytes:
    cal = Calendar()
    cal.add("prodid", PRODID)
    cal.add("version", "2.0")
    cal.add("calscale", "GREGORIAN")
    cal.add("x-wr-calname", "daylog")

    _goal_events(cal, goals_data)
    _itinerary_events(cal, itinerary_data)
    _journal_events(cal, journal_entries)

    ical_bytes: bytes = cal.to_ical()
    return ical_bytes
