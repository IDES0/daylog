"""What the app reads from the research routines, and journal search in chat."""

from __future__ import annotations

from datetime import date, datetime

from daylog import chat, services
from daylog.vault import Vault

TODAY = date(2026, 10, 28)


def test_brief_reads_every_routine_file_dated_today_morning_run_first(vault: Vault) -> None:
    vault.write_text("research/daily/2026-10-28-requests.md", "Looked into Valle.", "r")
    vault.write_text("research/daily/2026-10-28.md", "# Research — morning", "r")
    vault.write_text("research/daily/2026-10-27.md", "yesterday", "r")
    text = services.todays_research(vault, TODAY)
    assert text is not None
    assert text.startswith("# Research — morning")
    assert "<!-- research/daily/2026-10-28-requests.md -->\nLooked into Valle." in text
    assert "yesterday" not in text
    assert services.todays_research(vault, date(2026, 10, 30)) is None


def test_chat_lists_the_last_three_days_of_routine_files(vault: Vault) -> None:
    for name in ("2026-10-24", "2026-10-26", "2026-10-27", "2026-10-28", "2026-10-28-jobs"):
        vault.write_text(f"research/daily/{name}.md", "x", "r")
    listed = chat.Tools(vault, TODAY).t_list_research().splitlines()
    assert "daily/2026-10-28-jobs" in listed and "daily/2026-10-26" in listed
    assert "daily/2026-10-24" not in listed


def test_search_journal_finds_words_in_transcripts_newest_first(vault: Vault) -> None:
    vault.write_journal_entry(
        datetime(2026, 10, 20, 9, 0),
        {},
        "Thinking about Valle de Bravo for paragliding.",
        "Planning.",
    )
    vault.write_journal_entry(
        datetime(2026, 10, 25, 9, 0), {}, "Called the school in valle again.", "School call."
    )
    vault.write_journal_entry(datetime(2026, 10, 26, 9, 0), {}, "Surfed.", "Surf day.")
    tools = chat.Tools(vault, TODAY)
    out = tools.t_search_journal("Valle")
    assert out.index("## 2026-10-25") < out.index("## 2026-10-20")
    assert "2026-10-26" not in out
    assert "## 2026-10-20" not in tools.t_search_journal("valle school")
    assert tools.t_search_journal("kitesurf").startswith("Nothing in the journal")
