from __future__ import annotations

from datetime import date, datetime

from daylog.brief import _date_reference_table, current_location, recent_journal_summaries
from daylog.vault import Vault

LOCATION_HISTORY = [
    {
        "place": "Uluwatu, Bali, ID",
        "lat": -8.829,
        "lon": 115.0849,
        "from": "2026-08-09",
        "to": "2026-08-15",
    },
    {
        "place": "Kuta, Lombok, ID",
        "lat": -8.8948,
        "lon": 116.2832,
        "from": "2026-08-18",
        "to": None,
    },
]


def test_current_location_returns_open_ended_entry() -> None:
    entry = current_location(LOCATION_HISTORY)
    assert entry is not None
    assert entry["place"] == "Kuta, Lombok, ID"


def test_current_location_falls_back_to_last_when_none_open() -> None:
    all_closed = [{"place": "A", "from": "2026-01-01", "to": "2026-01-05"}]
    entry = current_location(all_closed)
    assert entry is not None
    assert entry["place"] == "A"


def test_current_location_none_for_empty_history() -> None:
    assert current_location([]) is None


def test_recent_journal_summaries_reads_last_n_days(vault: Vault) -> None:
    vault.write_journal_entry(datetime(2026, 8, 19, 9, 0), {}, "surfed", "Surfed at Echo Beach.")
    vault.write_journal_entry(datetime(2026, 8, 20, 9, 0), {}, "worked", "Applied to jobs.")

    text = recent_journal_summaries(vault, today=datetime(2026, 8, 21).date(), days=5)

    assert "2026-08-19: Surfed at Echo Beach." in text
    assert "2026-08-20: Applied to jobs." in text


def test_recent_journal_summaries_no_entries_returns_placeholder(vault: Vault) -> None:
    text = recent_journal_summaries(vault, today=datetime(2026, 8, 21).date(), days=5)
    assert text == "(no recent journal entries)"


def test_date_reference_table_labels_each_day_with_correct_weekday() -> None:
    # 2026-08-22 is a Saturday; 2026-08-25 is a Tuesday.
    table = _date_reference_table(date(2026, 8, 22), days=7)
    assert "2026-08-22 (Saturday) — today" in table
    assert "2026-08-25 (Tuesday)" in table
    assert "2026-08-29 (Saturday)" in table
