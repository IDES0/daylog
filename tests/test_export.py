from __future__ import annotations

import csv
import io
from datetime import date, datetime

from daylog import export
from daylog.vault import Vault


def _log(vault: Vault) -> None:
    vault.write_journal_entry(
        datetime(2026, 9, 25, 9, 0),
        {
            "activities": [
                {"type": "Surf", "hours": 2, "place": "lakey-peak"},
                {"type": "Scuba Diving", "hours": 1},
                {"type": "job search"},
            ],
            "meals": [
                {
                    "items": ["nasi goreng", "iced coffee"],
                    "when": "lunch",
                    "kcal": 750,
                    "protein_g": 25,
                    "carbs_g": 95,
                    "fat_g": 28,
                    "place": "the-field",
                },
                {"items": ["smoothie"], "kcal": 250, "protein_g": 5},
                {"items": ["mystery snack"]},
            ],
            "felt": [{"energy": 4, "mood": 5}, {"energy": 2, "focus": 3}],
        },
        "t",
        "s",
    )
    vault.write_journal_entry(
        datetime(2026, 9, 26, 9, 0),
        {"activities": [{"type": "surf", "hours": 1.5}], "meals": [{"items": ["toast"]}]},
        "t",
        "s",
    )


def test_day_rows_total_estimates_and_average_scores(vault: Vault) -> None:
    _log(vault)
    rows = export.day_rows(vault.read_journal_range(date(2026, 9, 25), date(2026, 9, 26)))

    first, second = rows
    assert first["date"] == "2026-09-25"
    assert (first["meals"], first["meals_estimated"]) == (3, 2)
    assert (first["kcal"], first["protein_g"], first["carbs_g"], first["fat_g"]) == (
        1000,
        30,
        95,
        28,
    )
    assert (first["energy"], first["mood"], first["focus"]) == (3.0, 5.0, 3.0)
    # synonyms collapse to one column; activities without hours add none
    assert (first["h_surf"], first["h_dive"]) == (2.0, 1.0)
    assert "h_job_search" not in first
    assert first["activities"] == 3

    # a day with no estimates leaves the nutrients empty rather than zero
    assert second["kcal"] is None and second["energy"] is None
    assert second["h_surf"] == 1.5


def test_meal_rows_one_per_meal(vault: Vault) -> None:
    _log(vault)
    rows = export.meal_rows(vault.read_journal_range(date(2026, 9, 25), date(2026, 9, 26)))

    assert len(rows) == 4
    assert rows[0]["items"] == "nasi goreng; iced coffee"
    assert (rows[0]["when"], rows[0]["place"], rows[0]["kcal"]) == ("lunch", "the-field", 750.0)
    assert rows[2]["kcal"] is None


def test_to_csv_puts_lead_columns_first_and_blanks_missing(vault: Vault) -> None:
    _log(vault)
    rows = export.day_rows(vault.read_journal_range(date(2026, 9, 25), date(2026, 9, 26)))
    parsed = list(csv.DictReader(io.StringIO(export.to_csv(rows, export.DAY_COLUMNS))))

    assert list(parsed[0])[: len(export.DAY_COLUMNS)] == list(export.DAY_COLUMNS)
    assert list(parsed[0])[len(export.DAY_COLUMNS) :] == ["h_dive", "h_surf"]
    assert parsed[1]["kcal"] == "" and parsed[1]["h_dive"] == ""
    assert parsed[0]["kcal"] == "1000"


def test_day_summary_only_with_estimates() -> None:
    assert export.day_summary({"meals": [{"items": ["toast"]}]}) is None
    assert (
        export.day_summary({"meals": [{"items": ["a"], "kcal": 1200, "protein_g": 60}]})
        == "Food (rough): ~1,200 kcal, 60 g protein"
    )
    assert export.day_summary({"meals": [{"items": ["a"], "kcal": 300}]}) == (
        "Food (rough): ~300 kcal"
    )
