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


def test_reconcile_carries_both_scores_and_keeps_the_users_own(
    vault: Vault, monkeypatch: pytest.MonkeyPatch
) -> None:
    _two_note_day(vault)
    entry = vault.read_journal_entry(DAY)
    assert entry is not None
    entry.frontmatter["rating"] = {"score": 8, "note": "great waves"}
    rebuilt = reconcile.rebuild(
        entry,
        {
            "activities": [{"type": "surf", "hours": 2}],
            "assessment": {"score": 6, "why": "One surf toward the goal; the rest drifted."},
            "summary": "Surfed.",
        },
    )
    # The pass missed the user's rating: it's kept, not wiped.
    assert rebuilt.frontmatter["rating"] == {"score": 8, "note": "great waves"}
    assert rebuilt.frontmatter["assessment"]["score"] == 6

    def fake_extract(transcript: str, *args: Any, **kwargs: Any) -> dict[str, Any]:
        return {
            "activities": [{"type": "surf", "hours": 2}],
            "rating": {"score": 7},
            "assessment": {"score": 6, "why": "A surf toward the goal."},
            "summary": "Surfed two hours.",
        }

    monkeypatch.setattr(extract, "extract", fake_extract)
    daily.run_reconcile(vault, DAY)
    entry = vault.read_journal_entry(DAY)
    assert entry is not None
    assert entry.frontmatter["rating"] == {"score": 7}
    assert entry.frontmatter["assessment"] == {"score": 6, "why": "A surf toward the goal."}
    assert not daily.needs_work(entry)


def test_a_single_note_day_is_only_assessed_not_rebuilt(
    vault: Vault, monkeypatch: pytest.MonkeyPatch
) -> None:
    day = date(2026, 9, 24)
    linked = [{"type": "surf", "hours": 2, "place": "lakey-peak"}]
    vault.write_journal_entry(datetime(2026, 9, 24, 20, 0), {"activities": linked}, "surfed", "S.")
    vault.write_journal_entry(
        datetime(2026, 9, 23, 20, 0), {}, "(Mentioned in the 2026-09-24 entry) ran", "Ran."
    )
    entry = vault.read_journal_entry(day)
    pointer = vault.read_journal_entry(date(2026, 9, 23))
    assert entry is not None and pointer is not None
    assert daily.needs_work(entry) and not reconcile.needs_reconcile(entry)
    assert not reconcile.needs_assessment(pointer)

    def fake_extract(transcript: str, *args: Any, **kwargs: Any) -> dict[str, Any]:
        assert kwargs["reconcile"] is True
        return {
            "activities": [{"type": "surf"}],  # a worse read: must not replace the record
            "assessment": {"score": 7, "why": "Two hours of surf toward the goal."},
            "summary": "Different summary.",
        }

    monkeypatch.setattr(extract, "extract", fake_extract)
    outcomes = daily.reconcile_recent(vault, date(2026, 9, 25))
    assert outcomes == ["2026-09-24 assessed: 7/10."]
    entry = vault.read_journal_entry(day)
    assert entry is not None
    assert entry.frontmatter["activities"] == linked
    assert entry.summary == "### 20:00\n\nS."
    assert entry.frontmatter["assessment"]["score"] == 7
    assert "reconciled" not in entry.frontmatter
    assert not daily.needs_work(entry)


def test_logical_day_rolls_back_before_the_cutoff() -> None:
    assert daily.logical_day(datetime(2026, 9, 26, 1, 30), cutoff_hour=4) == date(2026, 9, 25)
    assert daily.logical_day(datetime(2026, 9, 26, 4, 0), cutoff_hour=4) == date(2026, 9, 26)
    assert daily.logical_day(datetime(2026, 9, 26, 1, 30), cutoff_hour=0) == date(2026, 9, 26)


def test_rebuild_keeps_a_stated_time_the_re_read_lost(vault: Vault) -> None:
    vault.write_journal_entry(
        datetime(2026, 9, 25, 21, 0),
        {
            "activities": [
                {
                    "type": "surf",
                    "at": "16:00",
                    "time": "said",
                    "sessions": [{"at": "16:00"}, {"at": "18:00"}],
                }
            ]
        },
        "did the 4pm and 6pm sessions",
        "Two sessions.",
    )
    entry = vault.read_journal_entry(DAY)
    assert entry is not None
    result = reconcile.rebuild(
        entry, {"activities": [{"type": "surf", "at": "15:00", "hours": 2}], "summary": "Surfed."}
    )
    [surf] = result.frontmatter["activities"]
    assert surf["at"] == "16:00" and surf["time"] == "said" and len(surf["sessions"]) == 2
    assert surf["hours"] == 2
