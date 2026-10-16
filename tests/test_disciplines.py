from __future__ import annotations

from datetime import date
from typing import Any

from daylog import disciplines, reconcile
from daylog.disciplines import Graph
from daylog.places import PlaceIndex
from daylog.vault import JournalEntry, Vault


def _graph() -> Graph:
    return Graph(
        {
            "water": {"name": "Water"},
            "air": {"name": "Air"},
            "foil": {
                "name": "Foiling",
                "parents": ["water"],
                "status": "active",
                "aliases": ["foiling"],
                "resolve": {"by_place": {"austin": "wake_foil"}, "default": "tow_foil"},
            },
            "tow_foil": {"name": "Tow foil", "parents": ["foil"], "status": "active"},
            "wake_foil": {"name": "Wakefoil", "parents": ["foil"], "status": "active"},
            "wing_foil": {
                "name": "Wingfoil",
                "parents": ["foil", "air"],
                "status": "active",
                "aliases": ["wingfoiling"],
            },
            "kite": {"name": "Kiting", "parents": ["water", "air"], "status": "active"},
            "paraglide": {"name": "Paragliding", "parents": ["air"], "status": "active"},
            "speedwing": {
                "name": "Speedwinging",
                "parents": ["air"],
                "status": "want",
                "requires": [{"goal": "paragliding"}],
            },
            "snowkite": {
                "name": "Snowkiting",
                "parents": ["air"],
                "status": "want",
                "requires": ["kite"],
            },
        }
    )


def _places() -> PlaceIndex:
    return PlaceIndex(
        [
            {"id": "usa", "name": "United States", "kind": "country"},
            {"id": "texas", "name": "Texas", "kind": "region", "parent": "usa"},
            {"id": "austin", "name": "Austin", "kind": "town", "parent": "texas"},
            {"id": "lake-travis", "name": "Lake Travis", "kind": "beach", "parent": "austin"},
            {"id": "bali", "name": "Bali", "kind": "region", "parent": "usa"},
            {"id": "uluwatu", "name": "Uluwatu", "kind": "town", "parent": "bali"},
            {"id": "ulu-foil", "name": "Ulu foil", "kind": "surf_spot", "parent": "uluwatu"},
            {"id": "padang", "name": "Padang", "kind": "surf_spot", "parent": "uluwatu"},
        ]
    )


def test_lineage_counts_each_ancestor_once_across_two_paths() -> None:
    graph = _graph()
    assert graph.lineage("wing_foil") == {"wing_foil", "foil", "water", "air"}
    days = {
        date(2026, 10, 1): {"activities": [{"type": "wingfoiling", "hours": 2}]},
        date(2026, 10, 2): {"activities": [{"type": "kite", "hours": 1}, {"type": "travel"}]},
    }
    totals = disciplines.totals(graph, days)
    assert totals["water"].sessions == 2 and totals["water"].hours == 3
    assert totals["air"].sessions == 2
    assert totals["foil"].sessions == 1 and totals["foil"].last == date(2026, 10, 1)
    assert "travel" not in totals


def test_state_from_status_logs_and_prerequisites() -> None:
    graph = _graph()
    goals = [{"id": "paragliding", "title": "APPI 3", "status": "active"}]
    none: dict[str, disciplines.Totals] = {}
    assert disciplines.state(graph, "paraglide", goals, none) == "not started"
    assert disciplines.state(graph, "speedwing", goals, none) == "locked"
    assert disciplines.unmet(graph, "speedwing", goals, none) == ["APPI 3"]
    assert disciplines.state(graph, "snowkite", goals, none) == "locked"
    assert disciplines.state(graph, "water", goals, none) is None

    goals[0]["status"] = "done"
    kited = disciplines.totals(graph, {date(2026, 10, 1): {"activities": [{"type": "kite"}]}})
    assert disciplines.state(graph, "speedwing", goals, kited) == "ready"
    assert disciplines.state(graph, "snowkite", goals, kited) == "ready"
    assert disciplines.state(graph, "kite", goals, kited) == "active"


def test_vague_foil_uses_the_usual_at_the_spot_then_area_then_place_then_default() -> None:
    graph, places = _graph(), _places()

    def guess(place: str | None, history: list[tuple[str, str | None]]) -> dict[str, Any]:
        activity: dict[str, Any] = {"type": "foiling"}
        if place:
            activity["place"] = place
        disciplines.resolve_vague(graph, [activity], places, None, history)
        return activity

    usual = [("wing_foil", "ulu-foil"), ("tow_foil", "padang"), ("wing_foil", "ulu-foil")]
    assert guess("ulu-foil", usual) == {
        "type": "wing_foil",
        "guessed": "foiling",
        "place": "ulu-foil",
    }
    # a new spot in the same region: the usual around there
    assert guess("padang", [("wing_foil", "ulu-foil")])["type"] == "wing_foil"
    # no history: Austin and anything under it is wake, elsewhere tow
    assert guess("lake-travis", [])["type"] == "wake_foil"
    assert guess("ulu-foil", [])["type"] == "tow_foil"
    assert guess(None, [])["type"] == "tow_foil"


def test_vague_foil_falls_back_to_where_the_user_is() -> None:
    graph, places = _graph(), _places()
    activities: list[Any] = [{"type": "foil"}, {"type": "surf"}, {"type": "wake_foil"}]
    guesses = disciplines.resolve_vague(graph, activities, places, "austin", [])
    assert activities[0] == {"type": "wake_foil", "guessed": "foil"}
    assert activities[2] == {"type": "wake_foil"}
    assert len(guesses) == 1
    g = guesses[0]
    assert (g.index, g.said, g.chosen) == (0, "foil", "wake_foil")
    assert "Austin" in g.reason
    assert g.options == ["tow_foil", "wake_foil", "wing_foil"]


def test_reconcile_keeps_what_the_user_said_or_tapped() -> None:
    graph, places = _graph(), _places()
    old = {"activities": [{"type": "wing_foil"}, {"type": "tow_foil", "guessed": "foil"}]}
    prefer = disciplines.stated(graph, old)
    assert prefer == ["wing_foil"]
    activities: list[Any] = [{"type": "foil"}]
    assert disciplines.resolve_vague(graph, activities, places, "austin", [], prefer) == []
    assert activities == [{"type": "wing_foil"}]


def test_set_type_applies_only_to_the_same_guess() -> None:
    fm: dict[str, Any] = {"activities": [{"type": "tow_foil", "guessed": "foil"}]}
    assert not disciplines.set_type(fm, 0, "foiling", "wake_foil")
    assert not disciplines.set_type(fm, 3, "foil", "wake_foil")
    assert disciplines.set_type(fm, 0, "foil", "wake_foil")
    assert fm["activities"] == [{"type": "wake_foil", "confirmed": True}]


def test_an_unsaid_kind_goes_back_to_the_general_discipline() -> None:
    graph = _graph()
    # The 2026-10-15 note: "fell on my wing" is the hydrofoil's wing.
    activities: list[Any] = [{"type": "wing_foil"}, {"type": "kite"}]
    disciplines.unsaid_kinds(graph, activities, "foil session, I fell on my wing and cut my hand")
    assert activities == [{"type": "foil"}, {"type": "kite"}]

    said: list[Any] = [{"type": "wing_foil"}, {"type": "tow_foil"}]
    disciplines.unsaid_kinds(graph, said, "went wingfoiling, then a Tow foil session")
    assert said == [{"type": "wing_foil"}, {"type": "tow_foil"}]


def test_rebuild_keeps_said_or_tapped_kinds_only() -> None:
    graph = _graph()
    fm = {
        "activities": [
            {"type": "wing_foil"},  # the model's, never said
            {"type": "wake_foil", "confirmed": True},  # tapped
        ]
    }
    assert disciplines.stated(graph, fm, "fell on my wing") == ["wake_foil"]
    assert disciplines.stated(graph, fm) == ["wing_foil", "wake_foil"]


def test_goal_progress_needs_an_activity_under_the_goals_discipline() -> None:
    graph = Graph({**_graph().nodes, "surf": {"name": "Surfing", "parents": ["water"]}})
    goals = [
        {"id": "surf", "discipline": "surf"},
        {"id": "water", "discipline": "water"},
        {"id": "build"},
    ]
    progress = [
        {"goal_id": "surf", "delta": 1},
        {"goal_id": "water", "delta": 1},
        {"goal_id": "build", "delta": 1.5},
    ]
    kept, dropped = disciplines.fitting_progress(graph, goals, [{"type": "tow_foil"}], progress)
    assert [k["goal_id"] for k in kept] == ["water", "build"]
    assert dropped == [{"goal_id": "surf", "delta": 1}]


def test_outline_and_tree_render_every_node() -> None:
    graph = _graph()
    text = disciplines.outline(graph)
    assert "- water: Water" in text
    assert "  - wing_foil: Wingfoil (also under air; aka wingfoiling)" in text

    goals = [
        {"id": "paragliding", "title": "APPI 3", "status": "active", "discipline": "paraglide"}
    ]
    all_time = disciplines.totals(graph, {date(2026, 10, 1): {"activities": [{"type": "kite"}]}})
    view = disciplines.tree(graph, goals, {}, all_time, 30)
    assert "Kiting: 0 [1]" in view
    assert "🎯 APPI 3" in view
    assert "Speedwinging · 🔒 needs APPI 3" in view
    assert "↳ Wingfoil (under Foiling)" in view

    summary = disciplines.describe(graph, goals, {}, all_time, 30)
    assert "Paragliding — goal: APPI 3" in summary
    assert "Speedwinging — needs APPI 3" in summary


def test_rebuild_drops_new_discipline_proposals() -> None:
    entry = JournalEntry(date(2026, 10, 1), {}, "### 08:00\nx\n### 09:00\ny", "", raw="")
    facts = {
        "summary": "Sandboarded.",
        "activities": [{"type": "sandboard", "new_discipline_parents": ["earth"]}],
    }
    result = reconcile.rebuild(entry, facts)
    assert result.frontmatter["activities"] == [{"type": "sandboard"}]


def test_prepare_pulls_out_new_sports_and_narrows_vague_ones(vault: Vault) -> None:
    from daylog import discipline_flow

    graph = _graph()
    facts: dict[str, Any] = {
        "activities": [
            {"type": "sand boarding", "new_discipline_parents": ["earth"]},
            {"type": "foil", "place": "lake-travis"},
            {"type": "kite", "new_discipline_parents": ["water"]},
        ]
    }
    guesses, proposals = discipline_flow.prepare(
        vault, graph, facts, date(2026, 10, 15), _places(), None
    )
    assert [(p.node_id, p.name, p.parents) for p in proposals] == [
        ("sand_boarding", "Sand boarding", ["earth"])
    ]
    assert facts["activities"][1]["type"] == "wake_foil"
    assert [g.chosen for g in guesses] == ["wake_foil"]
    assert all("new_discipline_parents" not in a for a in facts["activities"])


def test_disciplines_round_trip_through_the_vault(vault: Vault) -> None:
    assert vault.read_disciplines() == {}
    vault.write_disciplines({"water": {"name": "Water"}}, "disciplines: seed")
    assert Graph(vault.read_disciplines()).name("water") == "Water"


def test_new_node_drops_unknown_parents() -> None:
    node = disciplines.new_node(_graph(), "Sandboarding", ["earth", "water"])
    assert node == {"name": "Sandboarding", "parents": ["water"], "status": "active"}
