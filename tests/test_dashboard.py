from __future__ import annotations

from datetime import date, datetime

from daylog import dashboard
from daylog.vault import Vault

TODAY = date(2026, 10, 2)


def test_upcoming_keeps_open_dated_legs_in_order() -> None:
    itinerary = [
        {"place": "Later", "status": "planned", "target_window": ["2026-11-08", "2026-11-27"]},
        {"place": "Exit", "status": "planned", "deadline": "2026-10-06"},
        {"place": "Now", "status": "planned", "target_window": ["2026-09-28", "2026-10-05"]},
        {"place": "Past", "status": "planned", "target_window": ["2026-09-01", "2026-09-05"]},
        {"place": "Done", "status": "done", "deadline": "2026-10-10"},
        {"place": "Undated", "status": "candidate"},
    ]
    assert dashboard.upcoming(itinerary, TODAY) == [
        ("Now", "Sep 28 – Oct 5"),
        ("Exit", "by Oct 6"),
        ("Later", "Nov 8 – Nov 27"),
    ]


def test_goal_rows_show_a_bar_only_with_a_target() -> None:
    rows = dashboard.goal_rows(
        [
            {"title": "Jobs", "status": "active", "progress": 82, "target": 150, "metric": "apps"},
            {"title": "Surf", "status": "active", "progress": 14, "metric": "sessions"},
            {"title": "Decide", "status": "active", "deadline": "2027-02-15"},
            {"title": "Old", "status": "done", "progress": 3},
        ]
    )
    assert [r["title"] for r in rows] == ["Jobs", "Surf", "Decide"]
    assert (rows[0]["value"], rows[0]["percent"]) == ("82 / 150", 55)
    assert (rows[1]["value"], rows[1]["percent"]) == ("14", None)
    assert (rows[2]["value"], rows[2]["when"]) == ("", "by Feb 15")


def test_render_is_one_escaped_page_without_scripts(vault: Vault) -> None:
    vault.write_yaml(
        "goals.yaml",
        [{"id": "s", "title": "Surf <rights>", "status": "active", "progress": 2, "target": 4}],
        "goals",
    )
    vault.write_journal_entry(
        datetime(2026, 10, 1, 9, 0),
        {
            "activities": [{"type": "surf", "hours": 2}],
            "meals": [{"items": ["rice"], "kcal": 600, "protein_g": 20}],
            "felt": [{"energy": 4}],
        },
        "t",
        "s",
    )
    html = dashboard.render(vault, TODAY)

    assert html.startswith("<!doctype html>")
    assert "Surf &lt;rights&gt;" in html and "<rights>" not in html
    assert "width:50%" in html
    assert "1 of 7" in html and "2 h" in html and "~600 kcal" in html
    assert "<script" not in html and "<form" not in html


def test_stay_rows_are_newest_first_with_what_was_done(vault: Vault) -> None:
    location = [
        {"place": "Bali", "from": "2026-09-01", "to": "2026-09-10"},
        {"place": "Lakey", "from": "2026-09-24", "to": None},
    ]
    vault.write_journal_entry(
        datetime(2026, 9, 5, 9, 0),
        {"activities": [{"type": "foil", "hours": 1}, {"type": "Surf", "hours": 3}]},
        "t",
        "s",
    )
    vault.write_journal_entry(
        datetime(2026, 9, 30, 9, 0), {"activities": [{"type": "surf", "hours": 2}]}, "t", "s"
    )
    entries = vault.read_journal_range(date(2026, 9, 1), TODAY)

    assert dashboard.stay_rows(location, entries, TODAY) == [
        {"place": "Lakey", "when": "Sep 24 – Oct 2 · now", "did": "surf 2 h"},
        {"place": "Bali", "when": "Sep 1 – Sep 10", "did": "surf 3 h · foil 1 h"},
    ]
