from __future__ import annotations

from datetime import date, datetime

from daylog import review
from daylog.vault import Vault


def test_week_bounds_and_label() -> None:
    assert review.week_bounds(date(2026, 9, 27)) == (date(2026, 9, 21), date(2026, 9, 27))
    assert review.week_bounds(date(2026, 9, 21)) == (date(2026, 9, 21), date(2026, 9, 27))
    assert review.week_label(date(2026, 9, 27)) == "2026-W39"


def test_week_stats_are_exact(vault: Vault) -> None:
    vault.write_journal_entry(
        datetime(2026, 9, 25, 20, 0),
        {
            "activities": [
                {"type": "Surf", "hours": 2, "place": "lakey-peak"},
                {"type": "screen_time", "hours": 2},
                {"type": "Scuba Diving", "hours": 1},
            ],
            "meals": [{"items": ["smoothie"]}],
            "felt": [{"energy": 4}],
            "goal_progress": [{"goal_id": "surf", "delta": 1}, {"goal_id": "jobs", "delta": 5}],
            "skipped": ["evening surf"],
        },
        "t",
        "s",
    )
    vault.write_journal_entry(
        datetime(2026, 9, 26, 20, 0),
        {
            "activities": [{"type": "surf", "hours": 2.5, "place": "lakey-peak"}],
            "felt": [{"energy": 2}],
            "goal_progress": [{"goal_id": "surf", "delta": 1}],
        },
        "t",
        "s",
    )
    stats = review.week_stats(vault.read_journal_range(date(2026, 9, 21), date(2026, 9, 27)))
    assert stats["days_logged"] == 2
    assert stats["activity_counts"] == {"surf": 2, "screen_time": 1, "dive": 1}
    assert stats["activity_hours"] == {"surf": 4.5, "screen_time": 2.0, "dive": 1.0}
    assert stats["goal_deltas"] == {"surf": 2.0, "jobs": 5.0}
    assert stats["avg_energy"] == 3.0
    assert stats["meals_logged"] == 1
    assert stats["places_visited"] == ["lakey-peak"]
    assert stats["skipped"] == ["evening surf"]

    text = review.build_input(
        vault.read_journal_range(date(2026, 9, 21), date(2026, 9, 27)), stats, "- surf", None, "$1"
    )
    assert "## 2026-09-25 (Friday)" in text and "(none — first review)" in text


def test_build_input_leads_with_principles(vault: Vault) -> None:
    vault.write_journal_entry(datetime(2026, 9, 25, 20, 0), {}, "t", "s")
    entries = vault.read_journal_range(date(2026, 9, 21), date(2026, 9, 27))
    text = review.build_input(entries, {}, "- surf", None, "$1", "Measure against own trajectory.")
    assert text.startswith("Their own operating principles (the lens for this review):\nMeasure")
