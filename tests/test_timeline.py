from __future__ import annotations

from typing import Any

from daylog import timeline


def _spans(blocks: list[timeline.Block]) -> list[tuple[str, float, float, str]]:
    return [(b.kind, b.start, b.end, b.time) for b in blocks]


def test_clock_reads_times_and_rejects_the_rest() -> None:
    assert timeline.clock("06:30") == 6.5
    assert timeline.clock("~7:15") == 7.25
    assert timeline.clock("morning") is None
    assert timeline.clock("25:00") is None
    assert timeline.clock(None) is None
    assert timeline.day_clock("02:00") == 26.0  # before the 4am cutoff: that night
    assert timeline.day_clock("04:00") == 4.0
    assert timeline.hhmm(26.5) == "02:30"


def test_untimed_activities_go_after_the_one_before_into_free_time() -> None:
    blocks = timeline.day_blocks(
        {
            "activities": [
                {"type": "work"},  # nothing to go on: the morning guess
                {"type": "surf", "at": "06:00", "hours": 2},
                {"type": "stretch", "hours": 0.1},  # after the surf, past the work
                {"type": "meal"},  # never on the calendar
            ]
        }
    )
    assert _spans(blocks) == [
        ("work", 8.0, 9.0, "guessed"),
        ("surf", 6.0, 8.0, "estimated"),
        ("stretch", 9.0, 9.5, "guessed"),
    ]
    assert blocks[0].guessed and not blocks[1].guessed


def test_an_untimed_first_item_ends_where_the_first_timed_one_starts() -> None:
    drive, surf = timeline.day_blocks(
        {"activities": [{"type": "travel", "hours": 2}, {"type": "surf", "at": "13:00"}]}
    )
    assert (drive.start, drive.end) == (11.0, 13.0)
    assert surf.start == 13.0


def test_untimed_items_skip_over_timed_ones_instead_of_piling_on() -> None:
    blocks = timeline.day_blocks(
        {
            "activities": [
                {"type": "work", "at": "09:00", "hours": 1},
                {"type": "errand", "hours": 1},
                {"type": "call", "at": "10:00", "hours": 2},
            ]
        }
    )
    errand = next(b for b in blocks if b.kind == "errand")
    assert (errand.start, errand.end) == (12.0, 13.0)
    assert all(b.lanes == 1 for b in blocks)


def test_stated_sessions_are_separate_blocks_at_their_own_times() -> None:
    blocks = timeline.day_blocks(
        {
            "activities": [
                {
                    "type": "surf",
                    "at": "16:00",
                    "hours": 3,
                    "time": "said",
                    "sessions": [{"at": "16:00"}, {"at": "18:00"}, {"at": "19:00"}],
                }
            ]
        }
    )
    assert _spans(blocks) == [
        ("surf", 16.0, 17.0, "said"),
        ("surf", 18.0, 19.0, "said"),
        ("surf", 19.0, 20.0, "said"),
    ]
    assert [b.session for b in blocks] == ["1/3", "2/3", "3/3"]


def test_an_end_time_sizes_the_block_and_runs_past_midnight() -> None:
    drive, games, reels = timeline.day_blocks(
        {
            "activities": [
                {"type": "travel", "at": "14:00", "until": "16:00", "hours": 9},
                {"type": "screen_time", "at": "23:30", "until": "02:00", "time": "said"},
                {"type": "reels", "until": "17:00", "hours": 1.5},
            ]
        }
    )
    assert (drive.start, drive.end) == (14.0, 16.0)  # the end wins over the hours
    assert (games.start, games.end, games.time) == (23.5, 26.0, "said")
    assert (reels.start, reels.end) == (15.5, 17.0)


def test_times_stay_on_the_day() -> None:
    late, night, dawn = timeline.day_blocks(
        {
            "activities": [
                {"type": "party", "at": "23:00", "hours": 7},
                {"type": "viewpoint", "at": "02:00"},  # after the party, not 2am that morning
                {"type": "build", "at": "04:00"},
            ]
        }
    )
    assert (late.start, late.end) == (23.0, timeline.DAY_END)
    assert night.start == 26.0
    assert dawn.start == 4.0


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


def test_tidy_times_normalises_what_the_model_wrote() -> None:
    a: dict[str, Any] = {
        "type": "surf",
        "at": "4pm",  # not HH:MM: dropped, then the first session supplies it
        "until": "later",
        "time": "said",
        "sessions": [{"at": "16:00"}, {"at": "18:00", "hours": 1}, {"note": "x"}, "junk"],
    }
    timeline.tidy_times(a)
    assert a == {
        "type": "surf",
        "at": "16:00",
        "time": "said",
        "sessions": [{"at": "16:00"}, {"at": "18:00", "hours": 1}],
    }
    one: dict[str, Any] = {"type": "sup", "sessions": [{"at": "17:00"}], "time": "maybe"}
    timeline.tidy_times(one)
    assert one == {"type": "sup", "at": "17:00"}
    bare: dict[str, Any] = {"type": "rest", "time": "said"}
    timeline.tidy_times(bare)
    assert bare == {"type": "rest"}


def test_keep_said_times_survives_a_vaguer_re_read() -> None:
    old = [
        {
            "type": "surf",
            "at": "16:00",
            "time": "said",
            "sessions": [{"at": "16:00"}, {"at": "18:00"}],
        },
        {"type": "work", "at": "09:00", "time": "estimated"},
    ]
    new: list[Any] = [
        {"type": "work", "at": "10:00"},
        {"type": "surf", "at": "15:00", "hours": 2, "time": "estimated"},
    ]
    assert timeline.keep_said_times(old, new) == 1
    assert new[0] == {"type": "work", "at": "10:00"}  # an estimate isn't kept over a new one
    assert new[1] == {
        "type": "surf",
        "at": "16:00",
        "hours": 2,
        "time": "said",
        "sessions": [{"at": "16:00"}, {"at": "18:00"}],
    }
