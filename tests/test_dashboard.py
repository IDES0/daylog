from __future__ import annotations

import json
from datetime import date, datetime

from daylog import dashboard
from daylog.vault import Vault

TODAY = date(2026, 10, 2)


def test_upcoming_keeps_legs_still_ahead_in_order() -> None:
    itinerary = [
        {"place": "Later", "status": "planned", "target_window": ["2026-11-08", "2026-11-27"]},
        {"place": "Exit", "status": "planned", "deadline": "2026-10-06"},
        {"place": "Now", "status": "planned", "target_window": ["2026-09-28", "2026-10-05"]},
        {"place": "Past", "status": "planned", "target_window": ["2026-09-01", "2026-09-05"]},
        {"place": "Done", "status": "done", "deadline": "2026-10-10"},
        {"place": "Undated", "status": "candidate"},
    ]
    # "Now" is under way (where you are, not where you're headed).
    assert dashboard.upcoming(itinerary, TODAY) == [
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


def test_render_is_one_escaped_page(vault: Vault) -> None:
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
            "milestones": [{"what": "23 m <depth> PB", "type": "freedive"}],
        },
        "t",
        "s",
    )
    html = dashboard.render(vault, TODAY, private=True)

    assert html.startswith("<!doctype html>")
    assert "Surf &lt;rights&gt;" in html and "<rights>" not in html
    assert "width:50%" in html
    assert "1 of 7" in html and "1 day · 2 h" in html and "~600 kcal" in html
    assert "23 m &lt;depth&gt; PB" in html


def test_guest_view_is_one_screen_of_where_and_next(vault: Vault) -> None:
    vault.write_yaml(
        "goals.yaml", [{"id": "s", "title": "Secret goal", "status": "active"}], "goals"
    )
    vault.write_yaml(
        "itinerary.yaml",
        [
            {"place": "Austin <TX>", "status": "planned", "deadline": "2026-10-05"},
            {"place": "Japan", "status": "planned", "target_window": ["2026-12-28", "2027-01-14"]},
        ],
        "itinerary",
    )
    vault.write_disciplines({"surf": {"name": "Surfing"}}, "disciplines")
    vault.write_journal_entry(
        datetime(2026, 10, 1, 9, 0),
        {
            "activities": [{"type": "surf", "hours": 2}, {"type": "job_search"}],
            "meals": [{"items": ["rice"], "kcal": 600}],
        },
        "t",
        "a private summary",
    )
    html = dashboard.render(vault, TODAY)

    assert '<main class="one">' in html
    assert "Austin &lt;TX&gt;" in html and "in 3 days" in html and "Japan" in html
    assert "<b>Surfing</b> 1 day" in html
    # Not on the guest screen: goals, food, non-sport activities, journal words.
    for private in ("Secret goal", "kcal", "job", "a private summary"):
        assert private not in html, private
    # No password configured: no password line.
    assert "<form" not in html


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
        datetime(2026, 9, 6, 9, 0), {"activities": [{"type": "surf"}, {"type": "travel"}]}, "t", "s"
    )
    vault.write_journal_entry(
        datetime(2026, 9, 30, 9, 0), {"activities": [{"type": "surf", "hours": 2}]}, "t", "s"
    )
    entries = vault.read_journal_range(date(2026, 9, 1), TODAY)

    assert dashboard.stay_rows(location, entries, TODAY) == [
        {"place": "Lakey", "when": "Sep 24 – Oct 2 · now", "did": "surf 1 day"},
        {"place": "Bali", "when": "Sep 1 – Sep 10", "did": "surf 2 days · foil 1 day"},
    ]


def test_activity_rows_count_days_and_show_hours_only_when_said() -> None:
    stats = {"activity_days": {"freedive": 2, "social": 2}, "activity_hours": {"freedive": 4.0}}
    assert dashboard.activity_rows(stats) == [
        ("freedive", "2 days · 4 h"),
        ("social", "2 days"),
    ]


def test_disciplines_graph_is_on_the_private_view_only(vault: Vault) -> None:
    vault.write_disciplines(
        {
            "water": {"name": "Water"},
            "air": {"name": "Air"},
            "foil": {"name": "Foiling", "parents": ["water"], "status": "active"},
            "wing_foil": {"name": "Wing <foil>", "parents": ["foil", "air"], "status": "active"},
            "speedwing": {
                "name": "Speedwinging",
                "parents": ["air"],
                "status": "want",
                "requires": [{"goal": "pg"}],
            },
        },
        "disciplines",
    )
    vault.write_yaml("goals.yaml", [{"id": "pg", "title": "APPI 3", "status": "active"}], "goals")
    vault.write_journal_entry(
        datetime(2026, 10, 1, 9, 0), {"activities": [{"type": "wing_foil", "hours": 2}]}, "t", "s"
    )

    assert 'id="discdata"' not in dashboard.render(vault, TODAY)
    html = dashboard.render(vault, TODAY, private=True)
    assert "<h2>Disciplines</h2>" in html
    assert "Wing &lt;foil&gt;" in html  # the list view escapes names
    assert "locked: needs APPI 3" in html
    payload = html.split('id="discdata">', 1)[1].split("</script>", 1)[0]
    data = json.loads(payload)
    wing = next(n for n in data["nodes"] if n["id"] == "wing_foil")
    assert (wing["parent"], wing["also"], wing["root"]) == ("foil", ["air"], "water")
    assert (wing["recent"], wing["total"], wing["state"]) == (1, 1, "active")
    assert data["slots"] == {"water": 1, "body": 2, "earth": 3, "air": 4}


def test_no_disciplines_file_means_no_section(vault: Vault) -> None:
    assert "<h2>Disciplines</h2>" not in dashboard.render(vault, TODAY, private=True)
