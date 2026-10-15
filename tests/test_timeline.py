from __future__ import annotations

from daylog import timeline


def test_clock_reads_rough_times_and_rejects_the_rest() -> None:
    assert timeline.clock("06:30") == 6.5
    assert timeline.clock("~7:15") == 7.25
    assert timeline.clock("morning") is None
    assert timeline.clock("25:00") is None
    assert timeline.clock(None) is None


def test_untimed_activities_follow_the_one_before_and_are_marked_guessed() -> None:
    blocks = timeline.day_blocks(
        {
            "activities": [
                {"type": "work"},  # nothing to go on: the first guess
                {"type": "surf", "at": "06:00", "hours": 2},
                {"type": "stretch", "hours": 0.1},  # straight after, at least half an hour
                {"type": "meal"},  # never on the calendar
            ]
        }
    )
    assert [(b.kind, b.start, b.end, b.guessed) for b in blocks] == [
        ("work", 8.0, 9.0, True),
        ("surf", 6.0, 8.0, False),
        ("stretch", 8.0, 8.5, True),
    ]


def test_times_stay_on_the_calendar() -> None:
    late, early = timeline.day_blocks(
        {"activities": [{"type": "party", "at": "23:50", "hours": 3}, {"type": "x", "at": "02:00"}]}
    )
    assert (late.start, late.end) == (23.5, 24.0)
    assert early.start == timeline.DAY_START


def test_overlaps_split_into_lanes_and_lone_blocks_take_the_width() -> None:
    blocks = timeline.day_blocks(
        {
            "activities": [
                {"type": "beach", "at": "10:00", "hours": 4},
                {"type": "call", "at": "11:00", "hours": 1},
                {"type": "nap", "at": "12:30", "hours": 1},
                {"type": "dinner_out", "at": "19:00", "hours": 2},
            ]
        }
    )
    lanes = {b.kind: (b.lane, b.lanes) for b in blocks}
    assert lanes == {
        "beach": (0, 2),
        "call": (1, 2),
        "nap": (1, 2),  # the call's lane is free again by 12:30
        "dinner_out": (0, 1),
    }
