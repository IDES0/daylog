from __future__ import annotations

from datetime import date
from typing import Any

from icalendar import Calendar

from daylog.calendar_feed import build_feed
from daylog.vault import JournalEntry


def _events(ics: bytes) -> list[dict[str, Any]]:
    cal = Calendar.from_ical(ics)
    return [dict(component) for component in cal.walk("VEVENT")]


def test_build_feed_is_valid_ics() -> None:
    ics = build_feed([], [], [])
    # Round-trips through the same library that produced it — a malformed
    # feed would raise here rather than silently reaching a calendar app.
    cal = Calendar.from_ical(ics)
    assert cal["prodid"] == "-//daylog//daylog//EN"


def test_build_feed_includes_hard_goal_deadline() -> None:
    goals = [
        {"id": "jobs", "title": "Job applications", "type": "hard", "deadline": date(2026, 10, 31)}
    ]
    events = _events(build_feed(goals, [], []))

    assert len(events) == 1
    assert "Deadline: Job applications" in str(events[0]["SUMMARY"])
    assert events[0]["DTSTART"].dt == date(2026, 10, 31)


def test_build_feed_includes_soft_goal_target() -> None:
    goals = [
        {
            "id": "surf",
            "title": "Get cracked at surfing",
            "type": "soft",
            "target_window": [date(2026, 10, 1), date(2026, 12, 31)],
        }
    ]
    events = _events(build_feed(goals, [], []))

    assert len(events) == 1
    assert "Target: Get cracked at surfing" in str(events[0]["SUMMARY"])
    assert events[0]["DTSTART"].dt == date(2026, 12, 31)


def test_build_feed_skips_dropped_goals() -> None:
    goals = [
        {"id": "x", "title": "X", "type": "hard", "deadline": date(2026, 1, 1), "status": "dropped"}
    ]
    assert _events(build_feed(goals, [], [])) == []


def test_build_feed_includes_itinerary_hard_deadline() -> None:
    itinerary = [
        {
            "id": "indonesia-visa-exit",
            "place": "Indonesia (exit)",
            "type": "hard",
            "deadline": date(2026, 10, 15),
        }
    ]
    events = _events(build_feed([], itinerary, []))

    assert len(events) == 1
    assert "Deadline: Indonesia (exit)" in str(events[0]["SUMMARY"])
    assert events[0]["DTSTART"].dt == date(2026, 10, 15)


def test_build_feed_includes_itinerary_soft_window_as_ranged_event() -> None:
    itinerary = [
        {
            "id": "canggu-bali-id",
            "place": "Canggu, Bali, ID",
            "type": "soft",
            "status": "current",
            "target_window": [date(2026, 8, 1), None],
        }
    ]
    events = _events(build_feed([], itinerary, []))

    assert len(events) == 1
    assert "Canggu, Bali, ID (current)" in str(events[0]["SUMMARY"])
    assert events[0]["DTSTART"].dt == date(2026, 8, 1)
    assert str(events[0]["STATUS"]) == "CONFIRMED"


def test_build_feed_candidate_itinerary_is_tentative() -> None:
    itinerary = [
        {
            "id": "flores",
            "place": "Flores, Indonesia",
            "type": "soft",
            "status": "candidate",
            "target_window": [date(2026, 11, 1), date(2026, 11, 15)],
        }
    ]
    events = _events(build_feed([], itinerary, []))

    assert str(events[0]["STATUS"]) == "TENTATIVE"


def test_build_feed_includes_journal_entry() -> None:
    entry = JournalEntry(
        date=date(2026, 8, 21),
        frontmatter={
            "activities": [{"type": "surf"}, {"type": "coding"}],
            "location": "Canggu, Bali",
        },
        transcript="raw",
        summary="### 09:14\n\nSurfed and worked on the outline.",
        raw="",
    )
    events = _events(build_feed([], [], [entry]))

    assert len(events) == 1
    assert str(events[0]["SUMMARY"]) == "surf, coding — Canggu, Bali"
    assert str(events[0]["DESCRIPTION"]) == "Surfed and worked on the outline."
    assert events[0]["DTSTART"].dt == date(2026, 8, 21)
    assert events[0]["DTEND"].dt == date(2026, 8, 22)


def test_build_feed_journal_entry_without_activities_falls_back() -> None:
    entry = JournalEntry(
        date=date(2026, 8, 21),
        frontmatter={},
        transcript="raw",
        summary="Rest day.",
        raw="",
    )
    events = _events(build_feed([], [], [entry]))

    assert str(events[0]["SUMMARY"]) == "journal entry"
