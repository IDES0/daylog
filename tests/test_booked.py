"""Bookings from email (research/times/<date>.yaml) on the calendar and in the brief."""

from __future__ import annotations

from datetime import date
from typing import Any
from zoneinfo import ZoneInfo

from daylog import booked, calendar_feed, timeline
from daylog.disciplines import Graph
from daylog.vault import Vault

DAY = date(2026, 11, 2)


def _graph() -> Graph:
    return Graph(
        {
            "water": {"name": "Water"},
            "surf": {"name": "Surfing", "parents": ["water"], "status": "active"},
        }
    )


SESSIONS: list[dict[str, Any]] = [
    {
        "id": "ws-1",
        "kind": "surf",
        "title": "Waco Surf session",
        "place": "Waco Surf",
        "at": "16:00",
        "until": "17:00",
        "source": "email: Waco Surf",
    },
    {
        "id": "ws-2",
        "kind": "Surf",
        "title": "Waco Surf session",
        "at": "18:00",
        "source": "email: Waco Surf",
    },
    {"id": "ws-3", "kind": "surf", "title": "cancelled one", "at": "19:00", "status": "cancelled"},
    {"id": "bad", "kind": "tour", "title": "no time"},
]


def test_clean_keeps_timed_bookings_and_drops_cancelled() -> None:
    found = booked.clean(SESSIONS)
    assert [(b["id"], b["at"], b["until"], b["kind"]) for b in found] == [
        ("ws-1", "16:00", "17:00", "surf"),
        ("ws-2", "18:00", "19:00", "surf"),  # an hour when no end is given
    ]
    assert booked.clean({"bookings": SESSIONS[:1]})[0]["place"] == "Waco Surf"
    assert booked.clean("nonsense") == []


def test_an_untimed_told_surf_takes_the_booked_time_and_spares_stand_alone() -> None:
    fm = {"activities": [{"type": "surf", "detail": "two sessions"}]}
    blocks = timeline.day_blocks(fm, None, None, _graph(), DAY, booked.clean(SESSIONS))
    surf, extra = sorted(blocks, key=lambda b: b.start)
    assert (surf.kind, surf.detail, surf.time, surf.start, surf.end) == (
        "surf",
        "two sessions",
        "booked",
        16.0,
        17.0,
    )
    assert surf.facts["source"] == "email: Waco Surf"
    # The second session matched nothing told: its own block, planned.
    assert (extra.kind, extra.detail, extra.time, extra.start) == (
        "surf",
        "Waco Surf session",
        "booked",
        18.0,
    )


def test_a_said_time_wins_over_a_booking_and_the_watch_wins_over_both() -> None:
    said = {"activities": [{"type": "surf", "at": "15:30", "hours": 1, "time": "said"}]}
    blocks = timeline.day_blocks(said, None, None, _graph(), DAY, booked.clean(SESSIONS[:1]))
    told = next(b for b in blocks if b.time == "said")
    assert told.start == 15.5
    assert any(b.time == "booked" and b.start == 16.0 for b in blocks)  # still shown
    watch = {
        "workouts": [
            {
                "id": "w",
                "kind": "Surfing",
                "start": f"{DAY} 16:05:00",
                "end": f"{DAY} 17:10:00",
                "minutes": 65,
            }
        ]
    }
    fm = {"activities": [{"type": "surf"}]}
    blocks = timeline.day_blocks(fm, watch, None, _graph(), DAY, booked.clean(SESSIONS[:1]))
    told = next(b for b in blocks if b.detail == "")
    assert told.time == "measured" and told.start == 16 + 5 / 60


def test_read_brief_and_feed(vault: Vault) -> None:
    vault.write_yaml(booked.day_file(DAY), SESSIONS, "times")
    found = booked.read(vault, DAY, DAY)
    assert list(found) == [DAY] and len(found[DAY]) == 2
    text = booked.brief_text(vault, DAY)
    assert (
        text.splitlines()[0]
        == "- Today 16:00–17:00: Waco Surf session (email: Waco Surf)"  # place in the title
    )
    assert booked.brief_text(vault, date(2026, 12, 1)) == ""
    blocks = timeline.day_blocks({}, None, None, _graph(), DAY, found[DAY])
    events = calendar_feed.block_events(DAY, blocks, ZoneInfo("America/Chicago"))
    assert [e["summary"] for e in events] == ["surf ✉", "surf ✉"]
    assert "confirmation email" in events[0]["description"]
