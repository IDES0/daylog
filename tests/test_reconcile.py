from __future__ import annotations

from datetime import date, datetime
from typing import Any

import pytest

from daylog import daily, extract, reconcile
from daylog.vault import Vault

DAY = date(2026, 9, 25)


def _two_note_day(vault: Vault) -> None:
    vault.write_journal_entry(
        datetime(2026, 9, 25, 7, 0),
        {
            "activities": [{"type": "surf", "detail": "going surfing"}],
            "goal_progress": [{"goal_id": "surf", "delta": 1}],
        },
        "heading out to surf",
        "Going surfing.",
    )
    vault.write_journal_entry(
        datetime(2026, 9, 25, 21, 0),
        {
            "activities": [{"type": "surf", "hours": 2}],
            "goal_progress": [{"goal_id": "surf", "delta": 1}],
        },
        "surfed two hours this morning",
        "Surfed two hours.",
    )


def test_needs_reconcile_only_for_multi_note_unreconciled_days(vault: Vault) -> None:
    vault.write_journal_entry(datetime(2026, 9, 24, 9, 0), {}, "one note", "One.")
    single = vault.read_journal_entry(date(2026, 9, 24))
    assert single is not None and not reconcile.needs_reconcile(single)

    _two_note_day(vault)
    entry = vault.read_journal_entry(DAY)
    assert entry is not None and reconcile.note_count(entry) == 2
    assert reconcile.needs_reconcile(entry)


def test_goal_adjustments_are_the_difference() -> None:
    old = [{"goal_id": "surf", "delta": 1}, {"goal_id": "surf", "delta": 1}]
    new = [{"goal_id": "surf", "delta": 1}, {"goal_id": "jobs", "delta": 5}]
    assert reconcile.goal_adjustments(old, new) == [
        {"goal_id": "jobs", "delta": 5.0},
        {"goal_id": "surf", "delta": -1.0},
    ]


def test_rebuild_drops_capture_only_fields_and_marks_reconciled(vault: Vault) -> None:
    _two_note_day(vault)
    entry = vault.read_journal_entry(DAY)
    assert entry is not None
    result = reconcile.rebuild(
        entry,
        {
            "activities": [{"type": "surf", "hours": 2}],
            "goal_progress": [{"goal_id": "surf", "delta": 1}],
            "itinerary_changes": [{"place": "Mentawai"}],
            "summary": "Surfed two hours.",
        },
    )
    assert result.frontmatter["reconciled"] is True
    assert "itinerary_changes" not in result.frontmatter
    assert result.goal_adjustments == [{"goal_id": "surf", "delta": -1.0}]


def test_run_reconcile_rewrites_day_and_corrects_goal_total(
    vault: Vault, monkeypatch: pytest.MonkeyPatch
) -> None:
    vault.write_goals([{"id": "surf", "title": "Surf", "progress": 2}], "goals: seed")
    _two_note_day(vault)

    def fake_extract(transcript: str, *args: Any, **kwargs: Any) -> dict[str, Any]:
        assert kwargs["reconcile"] is True
        assert "heading out to surf" in transcript and "surfed two hours" in transcript
        return {
            "activities": [{"type": "surf", "hours": 2}],
            "goal_progress": [{"goal_id": "surf", "delta": 1}],
            "summary": "Surfed two hours in the morning.",
        }

    monkeypatch.setattr(extract, "extract", fake_extract)
    outcome = daily.run_reconcile(vault, DAY)

    entry = vault.read_journal_entry(DAY)
    assert entry is not None
    assert entry.summary == "Surfed two hours in the morning."
    assert entry.frontmatter["activities"] == [{"type": "surf", "hours": 2}]
    assert entry.frontmatter["reconciled"] is True
    assert "### 07:00" in entry.transcript and "### 21:00" in entry.transcript
    assert vault.read_goals()[0]["progress"] == 1
    assert "Surf -1" in outcome

    # A late note re-opens the day for reconciling.
    vault.write_journal_entry(datetime(2026, 9, 25, 23, 0), {}, "also ran", "Ran.")
    entry = vault.read_journal_entry(DAY)
    assert entry is not None and reconcile.needs_reconcile(entry)


def test_logical_day_rolls_back_before_the_cutoff() -> None:
    assert daily.logical_day(datetime(2026, 9, 26, 1, 30), cutoff_hour=4) == date(2026, 9, 25)
    assert daily.logical_day(datetime(2026, 9, 26, 4, 0), cutoff_hour=4) == date(2026, 9, 26)
    assert daily.logical_day(datetime(2026, 9, 26, 1, 30), cutoff_hour=0) == date(2026, 9, 26)
