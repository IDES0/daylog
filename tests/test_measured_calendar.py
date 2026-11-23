"""Watch workouts, sleep and screen time on the week calendar (invented data)."""

from __future__ import annotations

from datetime import date, datetime

from daylog import measures, timeline
from daylog.disciplines import Graph

DAY = date(2026, 11, 20)
NEXT = date(2026, 11, 21)


def _graph() -> Graph:
    return Graph(
        {
            "water": {"name": "Water"},
            "body": {"name": "Body"},
            "surf": {"name": "Surfing", "parents": ["water"], "status": "active"},
            "foil": {"name": "Foiling", "parents": ["water"], "status": "active"},
            "wing_foil": {"name": "Wingfoil", "parents": ["foil"], "status": "active"},
            "gym": {"name": "Gym", "parents": ["body"], "status": "active"},
        }
    )


def _w(kind: str, start: str, end: str, **extra: object) -> dict:
    return {"id": f"{kind}-{start}", "kind": kind, "start": start, "end": end, **extra}


SURF = _w(
    "Surfing",
    "2026-11-20 06:40:00 -0600",
    "2026-11-20 08:25:00 -0600",
    minutes=105,
    hr_avg=128,
    kcal=500,
)
WALK = _w("Walking", "2026-11-20 18:00:00 -0600", "2026-11-20 18:45:00 -0600", minutes=45)


def by_layer(blocks: list[timeline.Block], layer: str) -> list[timeline.Block]:
    return [b for b in blocks if b.layer == layer]


def test_a_told_surf_takes_the_watch_times() -> None:
    fm = {"activities": [{"type": "surf", "at": "08:00", "hours": 2, "detail": "big day"}]}
    blocks = timeline.day_blocks(fm, {"workouts": [SURF, WALK]}, None, _graph(), DAY)
    surf, walk = by_layer(blocks, "activity")
    assert (surf.kind, surf.detail, surf.time, surf.source) == (
        "surf",
        "big day",
        "measured",
        "watch",
    )
    assert (surf.start, surf.end) == (6 + 40 / 60, 8 + 25 / 60)
    assert surf.facts["hr_avg"] == 128
    # The walk was never told: it gets its own block, under Apple's name.
    assert (walk.kind, walk.time, walk.start) == ("walking", "measured", 18.0)


def test_without_measurements_the_journal_alone_is_unchanged() -> None:
    fm = {"activities": [{"type": "surf", "at": "08:00", "hours": 2}]}
    assert timeline.day_blocks(fm) == timeline.day_blocks(fm, None, None, _graph(), DAY)
    (b,) = timeline.day_blocks(fm)
    assert (b.start, b.time) == (8.0, "estimated")


def test_a_far_off_workout_of_the_same_sport_is_not_the_told_one() -> None:
    fm = {"activities": [{"type": "surf", "at": "16:00", "hours": 1}]}
    blocks = timeline.day_blocks(fm, {"workouts": [SURF]}, None, _graph(), DAY)
    assert sorted((b.kind, b.time) for b in blocks) == [
        ("surf", "estimated"),
        ("surfing", "measured"),
    ]


def test_generic_workouts_match_a_sport_only_by_overlap_and_untimed_by_sport() -> None:
    other = _w("Water Sports", "2026-11-20 10:00:00 -0600", "2026-11-20 11:30:00 -0600")
    fm = {
        "activities": [
            {"type": "wing_foil", "at": "10:30", "hours": 1},
            {"type": "gym"},  # untimed: matched by sport alone
        ]
    }
    lift = _w(
        "Traditional Strength Training", "2026-11-20 17:00:00 -0600", "2026-11-20 18:00:00 -0600"
    )
    blocks = timeline.day_blocks(fm, {"workouts": [other, lift]}, None, _graph(), DAY)
    wing, gym = by_layer(blocks, "activity")
    assert (wing.kind, wing.start, wing.end, wing.time) == ("wing_foil", 10.0, 11.5, "measured")
    assert (gym.kind, gym.start, gym.time) == ("gym", 17.0, "measured")


def test_sleep_is_split_across_logical_days_and_guesses_avoid_it() -> None:
    last_night = {
        "start": "2026-11-19 23:30:00 -0600",
        "end": "2026-11-20 08:30:00 -0600",
        "asleep_h": 8.2,
    }
    tonight = {
        "start": "2026-11-20 23:00:00 -0600",
        "end": "2026-11-21 06:00:00 -0600",
        "asleep_h": 6.5,
    }
    fm = {"activities": [{"type": "gym"}]}  # untimed: the morning guess is 8:00
    blocks = timeline.day_blocks(fm, {"sleep": [last_night]}, {"sleep": [tonight]}, _graph(), DAY)
    morning, evening = by_layer(blocks, "sleep")
    assert (morning.start, morning.end) == (4.0, 8.5)  # from the cutoff to waking
    assert (evening.start, evening.end) == (23.0, 28.0)  # to the next cutoff
    assert morning.facts["from"] == "23:30" and morning.facts["asleep_h"] == 8.2
    (gym,) = by_layer(blocks, "activity")
    assert gym.time == "guessed" and gym.start == 8.5


def test_screen_time_is_a_coalesced_strip_including_after_midnight() -> None:
    def s(app: str, start: str, end: str, site: str = "") -> dict:
        return {"device": "mac", "app": app, "site": site, "start": start, "end": end}

    today = {
        "screen": [
            s("Code", "2026-11-20 09:00:00 -0600", "2026-11-20 09:50:00 -0600"),
            s("Dia", "2026-11-20 09:55:00 -0600", "2026-11-20 10:20:00 -0600", "github.com"),
            s("Messages", "2026-11-20 13:00:00 -0600", "2026-11-20 13:02:00 -0600"),  # too short
        ]
    }
    after_midnight = {
        "screen": [
            s("Dia", "2026-11-21 00:10:00 -0600", "2026-11-21 01:40:00 -0600", "youtube.com")
        ],
        "screen_events": [
            {
                "device": "iphone",
                "app": "Instagram",
                "event": "open",
                "at": "2026-11-21 00:30:00 -0600",
            },
            {
                "device": "iphone",
                "app": "Instagram",
                "event": "close",
                "at": "2026-11-21 00:55:00 -0600",
            },
        ],
    }
    blocks = by_layer(timeline.day_blocks({}, today, after_midnight, _graph(), DAY), "screen")
    work, late, phone = sorted(blocks, key=lambda b: (b.start, b.source))
    assert (work.start, work.end, work.detail) == (9.0, 10 + 20 / 60, "Code")
    assert work.facts["apps"] == {"Code": 50, "github.com": 25}
    assert (late.start, late.end, late.detail, late.source) == (
        24 + 10 / 60,
        25 + 40 / 60,
        "youtube.com",
        "mac",
    )
    assert (phone.source, phone.detail, phone.facts["minutes"]) == ("iphone", "Instagram", 25)


def test_paired_events_cap_an_unclosed_open() -> None:
    sessions = measures.paired_events(
        [
            {"app": "TikTok", "event": "open", "at": "2026-11-20 22:00:00"},
            {"app": "Maps", "event": "open", "at": "2026-11-20 22:10:00"},
            {"app": "Maps", "event": "close", "at": "2026-11-20 22:15:00"},
            {"app": "YouTube", "event": "open", "at": "2026-11-20 23:00:00"},
        ]
    )
    assert [(x.app, x.minutes) for x in sessions] == [("TikTok", 10), ("Maps", 5), ("YouTube", 30)]


def test_screen_minutes_prefers_a_devices_daily_totals() -> None:
    records = {
        "screen": [
            {
                "device": "mac",
                "app": "Code",
                "start": "2026-11-20 09:00:00",
                "end": "2026-11-20 10:00:00",
            }
        ],
        "screen_events": [
            {"device": "iphone", "app": "Instagram", "event": "open", "at": "2026-11-20 22:00:00"},
            {"device": "iphone", "app": "Instagram", "event": "close", "at": "2026-11-20 22:20:00"},
        ],
        "screen_totals": [
            {"device": "iphone", "app": "Instagram", "minutes": 75},
            {"device": "iphone", "app": "Safari", "minutes": 30},
        ],
    }
    assert measures.screen_minutes(records) == {
        "iphone": {"Instagram": 75.0, "Safari": 30.0},
        "mac": {"Code": 60.0},
    }


def test_same_sport_through_the_graph() -> None:
    g = _graph()
    assert measures.same_sport(g, "surf", "Surfing")
    assert measures.same_sport(None, "gym", "Functional Strength Training")
    assert not measures.same_sport(g, "wing_foil", "Surfing")
    assert not measures.same_sport(g, "surf", "Other")
    assert measures.clock_hours(datetime(2026, 11, 21, 1, 30), DAY) == 25.5
