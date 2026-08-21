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
