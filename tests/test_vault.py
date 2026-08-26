from __future__ import annotations

import subprocess
from datetime import date, datetime
from pathlib import Path

import pytest

from daylog.vault import Vault, VaultError

FRONTMATTER = {
    "location": "Canggu, Bali",
    "activities": [
        {"type": "surf", "hours": 2.0, "detail": "Echo Beach, chest high, crowded"},
    ],
    "mood": "good",
    "skipped": ["gym"],
    "open_questions": ["worth paying for Surfline premium?"],
}

ENTRY_TIME = datetime(2026, 8, 21, 9, 14)


def test_journal_path(vault: Vault) -> None:
    assert vault.journal_path(date(2026, 8, 21)) == vault.path / "journal" / "2026-08-21.md"


def test_write_journal_entry_creates_file(vault: Vault) -> None:
    path = vault.write_journal_entry(
        ENTRY_TIME, FRONTMATTER, "raw transcript text", "Surfed and worked."
    )

    assert path.exists()
    text = path.read_text()
    assert text.startswith("---\n")
    assert "date: 2026-08-21" in text
    assert "location: Canggu, Bali" in text
    assert "## Transcript" in text
    assert "### 09:14" in text
    assert "raw transcript text" in text
    assert "## Summary" in text
    assert "Surfed and worked." in text


def test_write_journal_entry_field_order(vault: Vault) -> None:
    path = vault.write_journal_entry(ENTRY_TIME, FRONTMATTER, "t", "s")
    text = path.read_text()

    frontmatter_block = text.split("---")[1]
    order = [
        line.split(":")[0]
        for line in frontmatter_block.splitlines()
        if line and not line.startswith((" ", "-"))
    ]
    assert order == ["date", "location", "activities", "skipped", "mood", "open_questions"]


def test_write_journal_entry_commits(vault: Vault) -> None:
    vault.write_journal_entry(ENTRY_TIME, FRONTMATTER, "t", "s")

    log = subprocess.run(
        ["git", "log", "-1", "--pretty=%s"],
        cwd=vault.path,
        capture_output=True,
        text=True,
        check=True,
    )
    assert log.stdout.strip() == "journal: 2026-08-21"

    status = subprocess.run(
        ["git", "status", "--porcelain"],
        cwd=vault.path,
        capture_output=True,
        text=True,
        check=True,
    )
    assert status.stdout == ""


def test_write_journal_entry_push_failure_is_swallowed(vault: Vault) -> None:
    # No remote configured: push will fail, but the write must not raise
    # (this is the offline case — commit locally, retry push next time).
    path = vault.write_journal_entry(ENTRY_TIME, FRONTMATTER, "t", "s")
    assert path.exists()


def test_write_journal_entry_pushes_to_remote(
    vault_with_remote: tuple[Vault, Path],
) -> None:
    vault, remote = vault_with_remote
    vault.write_journal_entry(ENTRY_TIME, FRONTMATTER, "t", "s")

    log = subprocess.run(
        ["git", "log", "-1", "--pretty=%s", "main"],
        cwd=remote,
        capture_output=True,
        text=True,
        check=True,
    )
    assert log.stdout.strip() == "journal: 2026-08-21"


def test_read_journal_entry_roundtrip(vault: Vault) -> None:
    vault.write_journal_entry(ENTRY_TIME, FRONTMATTER, "raw transcript text", "Surfed and worked.")

    entry = vault.read_journal_entry(ENTRY_TIME.date())

    assert entry is not None
    assert entry.date == ENTRY_TIME.date()
    assert entry.frontmatter["location"] == "Canggu, Bali"
    assert entry.frontmatter["mood"] == "good"
    assert entry.frontmatter["activities"][0]["type"] == "surf"
    assert "### 09:14" in entry.transcript
    assert "raw transcript text" in entry.transcript
    assert "### 09:14" in entry.summary
    assert "Surfed and worked." in entry.summary


def test_read_journal_entry_missing_returns_none(vault: Vault) -> None:
    assert vault.read_journal_entry(date(2099, 1, 1)) is None


def test_commit_failure_raises(vault: Vault, monkeypatch: pytest.MonkeyPatch) -> None:
    # Simulate a broken repo (e.g. corrupted .git) by pointing at a non-repo dir.
    broken = Vault(vault.path.parent / "not-a-repo")
    broken.path.mkdir()

    with pytest.raises(VaultError):
        broken.write_journal_entry(ENTRY_TIME, FRONTMATTER, "t", "s")


def test_commit_failure_message_includes_stdout_reason(vault: Vault) -> None:
    # `git commit` with nothing staged prints its reason ("nothing to
    # commit, working tree clean") to stdout, not stderr — the error
    # message must still surface it, or a real failure like this is
    # undiagnosable from logs.
    target = vault.path / "unchanged.txt"
    target.write_text("x", encoding="utf-8")
    vault._commit(target, "seed")  # first commit succeeds, nothing left to stage next time

    with pytest.raises(VaultError, match="nothing to commit"):
        vault._commit(target, "no-op")


def test_second_entry_same_day_appends_not_overwrites(vault: Vault) -> None:
    vault.write_journal_entry(ENTRY_TIME, FRONTMATTER, "morning transcript", "Morning summary.")

    afternoon = datetime(2026, 8, 21, 14, 32)
    afternoon_facts = {
        "activities": [{"type": "deep_work", "hours": 3.0, "detail": "system design"}],
        "skipped": ["gym"],  # duplicate of the morning entry's skip — should not double up
        "mood": "focused",
    }
    path = vault.write_journal_entry(
        afternoon, afternoon_facts, "afternoon transcript", "Afternoon summary."
    )

    text = path.read_text()
    # Both entries' content survives.
    assert "### 09:14" in text
    assert "morning transcript" in text
    assert "Morning summary." in text
    assert "### 14:32" in text
    assert "afternoon transcript" in text
    assert "Afternoon summary." in text

    entry = vault.read_journal_entry(ENTRY_TIME.date())
    assert entry is not None
    # activities from both entries are present
    types = [a["type"] for a in entry.frontmatter["activities"]]
    assert types == ["surf", "deep_work"]
    # skipped is deduped, not doubled
    assert entry.frontmatter["skipped"] == ["gym"]
    # mood took the latest value
    assert entry.frontmatter["mood"] == "focused"
    # location wasn't touched by the second entry, so the first value survives
    assert entry.frontmatter["location"] == "Canggu, Bali"


def test_second_entry_same_day_uses_append_commit_message(vault: Vault) -> None:
    vault.write_journal_entry(ENTRY_TIME, FRONTMATTER, "t1", "s1")
    vault.write_journal_entry(datetime(2026, 8, 21, 14, 32), {}, "t2", "s2")

    log = subprocess.run(
        ["git", "log", "-1", "--pretty=%s"],
        cwd=vault.path,
        capture_output=True,
        text=True,
        check=True,
    )
    assert log.stdout.strip() == "journal: 2026-08-21 (+entry)"


GOALS_YAML = """\
# Hand-edit freely. Bot appends progress / slip_history; never silently moves hard deadlines.

- id: jobs
  title: Job applications
  type: hard
  # deadline: YYYY-MM-DD
  metric: applications_sent
  progress: 0
  status: active

- id: surf
  title: Get cracked at surfing
  type: soft
  status: active
  slip_history: []
  notes: qualitative — hours in water, comfort in bigger/crowded waves
"""


def test_read_goals_missing_file_returns_empty_list(vault: Vault) -> None:
    assert vault.read_goals() == []


def test_read_goals_parses_existing_file(vault: Vault) -> None:
    vault.goals_path.write_text(GOALS_YAML, encoding="utf-8")

    goals = vault.read_goals()

    assert len(goals) == 2
    assert goals[0]["id"] == "jobs"
    assert goals[0]["progress"] == 0
    assert goals[1]["id"] == "surf"


def test_write_goals_preserves_comments_and_applies_edit(vault: Vault) -> None:
    vault.goals_path.write_text(GOALS_YAML, encoding="utf-8")

    goals = vault.read_goals()
    goals[0]["progress"] = 3  # mutate the live ruamel structure in place

    vault.write_goals(goals, "goals: +3 jobs")

    text = vault.goals_path.read_text(encoding="utf-8")
    assert "# Hand-edit freely." in text
    assert "# deadline: YYYY-MM-DD" in text
    assert "progress: 3" in text
    # comments anchored to a field must stay attached to that field, not
    # float somewhere else in the file after a round-trip
    assert text.index("# deadline: YYYY-MM-DD") < text.index("metric: applications_sent")


def test_write_goals_commits(vault: Vault) -> None:
    vault.goals_path.write_text(GOALS_YAML, encoding="utf-8")
    goals = vault.read_goals()

    vault.write_goals(goals, "goals: +3 jobs")

    log = subprocess.run(
        ["git", "log", "-1", "--pretty=%s"],
        cwd=vault.path,
        capture_output=True,
        text=True,
        check=True,
    )
    assert log.stdout.strip() == "goals: +3 jobs"


ITINERARY_YAML = """\
# Hand-edit freely. Bot appends entries / slip_history; never silently moves hard dates.

- id: canggu-bali-id
  place: Canggu, Bali, ID
  type: soft
  status: current
  target_window: [2026-08-01, null]

- id: indonesia-visa-exit
  place: Indonesia (exit)
  type: hard
  # deadline: YYYY-MM-DD
  status: active
"""


def test_read_itinerary_missing_file_returns_empty_list(vault: Vault) -> None:
    assert vault.read_itinerary() == []


def test_read_itinerary_parses_existing_file(vault: Vault) -> None:
    vault.itinerary_path.write_text(ITINERARY_YAML, encoding="utf-8")

    itinerary = vault.read_itinerary()

    assert len(itinerary) == 2
    assert itinerary[0]["id"] == "canggu-bali-id"
    assert itinerary[1]["id"] == "indonesia-visa-exit"


def test_write_itinerary_preserves_comments_and_applies_edit(vault: Vault) -> None:
    vault.itinerary_path.write_text(ITINERARY_YAML, encoding="utf-8")

    itinerary = vault.read_itinerary()
    # A real `date` object, not a string — matches how goals.py/itinerary.py
    # write dates (see their _write_slip/_write_date), so this dumps
    # unquoted like a hand-typed date rather than `deadline: '2026-10-15'`.
    itinerary[1]["deadline"] = date(2026, 10, 15)

    vault.write_itinerary(itinerary, "itinerary: Indonesia (exit)")

    text = vault.itinerary_path.read_text(encoding="utf-8")
    assert "# Hand-edit freely." in text
    assert "# deadline: YYYY-MM-DD" in text
    assert "deadline: 2026-10-15" in text  # unquoted
    # `deadline` didn't exist as a real key before, only as a comment, so
    # ruamel appends the new key after the existing ones rather than
    # relocating the (unrelated, position-anchored) comment next to it —
    # the comment stays exactly where it was, between type and status.
    assert (
        text.index("type: hard")
        < text.index("# deadline: YYYY-MM-DD")
        < text.index("status: active")
    )


def test_write_itinerary_commits(vault: Vault) -> None:
    vault.itinerary_path.write_text(ITINERARY_YAML, encoding="utf-8")
    itinerary = vault.read_itinerary()

    vault.write_itinerary(itinerary, "itinerary: Indonesia (exit)")

    log = subprocess.run(
        ["git", "log", "-1", "--pretty=%s"],
        cwd=vault.path,
        capture_output=True,
        text=True,
        check=True,
    )
    assert log.stdout.strip() == "itinerary: Indonesia (exit)"
