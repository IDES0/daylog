from __future__ import annotations

from datetime import date, datetime
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

    graph = _graph()
    facts: dict[str, Any] = {
        "activities": [
            {"type": "sand boarding", "new_discipline_parents": ["earth"]},
            {"type": "foil", "place": "lake-travis"},
            {"type": "kite", "new_discipline_parents": ["water"]},
        ]
    }
    guesses, proposals = disciplines.prepare(
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


def test_a_misheard_sport_is_logged_as_meant_and_can_be_switched_back() -> None:
    graph = Graph({**_graph().nodes, "surf": {"name": "Surfing", "parents": ["water"]}})
    activities: list[Any] = [
        {"type": "surf", "heard_as": "Wakefoil"},
        {"type": "surf", "heard_as": "surfing"},
        {"type": "tow_foil", "heard_as": "a wave pool"},
    ]
    guesses = disciplines.misheard(graph, activities)
    assert activities[0] == {"type": "surf", "guessed": "wake_foil"}
    assert activities[1] == {"type": "surf"}
    assert activities[2] == {"type": "tow_foil"}
    assert [(g.said, g.chosen, g.options) for g in guesses] == [
        ("wake_foil", "surf", ["surf", "wake_foil"])
    ]
    fm = {"activities": activities}
    assert disciplines.set_type(fm, 0, "wake_foil", "wake_foil")
    assert fm["activities"][0]["type"] == "wake_foil"


def test_a_sessions_goal_is_counted_from_the_journal() -> None:
    from daylog import goals as goals_mod

    graph = Graph({**_graph().nodes, "surf": {"name": "Surfing", "parents": ["water"]}})
    goals_data = [
        {"id": "surf", "discipline": "surf", "metric": "sessions", "progress": 3, "baseline": 10},
        {"id": "fly", "discipline": "paraglide", "metric": "flights", "progress": 4},
        {"id": "jobs", "metric": "applications_sent", "progress": 85},
    ]
    days = {
        date(2026, 11, 2): {"activities": [{"type": "surf"}, {"type": "wake_foil"}]},
        date(2026, 11, 3): {"activities": [{"type": "surfing"}, {"type": "surf"}]},
    }
    live = disciplines.count_goals(graph, goals_data, days)
    assert [g["progress"] for g in live] == [13, 4, 85]
    assert goals_data[0]["progress"] == 3
    # A note's +1 doesn't double-count a counted goal; other goals still move.
    applied = goals_mod.apply_progress(
        goals_data, [{"goal_id": "surf", "delta": 1}, {"goal_id": "jobs", "delta": 2}]
    )
    assert [a.goal_id for a in applied] == ["jobs"]
    assert goals_data[0]["progress"] == 3


# -- where: sessions by place ---------------------------------------------------


def _surf_places() -> PlaceIndex:
    return PlaceIndex(
        [
            {"id": "indonesia", "name": "Indonesia", "kind": "country"},
            {"id": "bali", "name": "Bali", "kind": "region", "parent": "indonesia"},
            {"id": "uluwatu", "name": "Uluwatu", "kind": "town", "parent": "bali"},
            {"id": "impossibles", "name": "Impossibles", "kind": "surf_spot", "parent": "uluwatu"},
            {"id": "sumbawa", "name": "Sumbawa", "kind": "region", "parent": "indonesia"},
            {"id": "lakey", "name": "Lakey", "kind": "town", "parent": "sumbawa"},
            {"id": "lakey-peak", "name": "Lakey Peak", "kind": "surf_spot", "parent": "lakey"},
            {"id": "usa", "name": "United States", "kind": "country"},
            {"id": "texas", "name": "Texas", "kind": "region", "parent": "usa"},
            {"id": "waco", "name": "Waco", "kind": "town", "parent": "texas"},
            {"id": "pool", "name": "Wave Pool", "kind": "wave_pool", "parent": "waco"},
        ]
    )


def _surf_graph() -> Graph:
    return Graph(
        {
            "water": {"name": "Water"},
            "surf": {
                "name": "Surfing",
                "parents": ["water"],
                "status": "active",
                "aliases": ["surfing"],
            },
            "foil": {"name": "Foiling", "parents": ["water"], "status": "active"},
        }
    )


def _surf_days() -> dict[date, dict[str, Any]]:
    return {
        date(2026, 9, 25): {"activities": [{"type": "surf", "place": "lakey-peak", "hours": 2}]},
        date(2026, 9, 26): {"activities": [{"type": "surf", "place": "lakey-peak"}]},
        date(2026, 10, 2): {"activities": [{"type": "surf", "place": "lakey"}]},
        date(2026, 10, 11): {
            "activities": [
                {"type": "surf", "place": "impossibles", "hours": 1.5},
                {"type": "foil", "place": "uluwatu"},
                {"type": "travel", "place": "uluwatu"},
            ]
        },
        date(2026, 11, 2): {
            "activities": [
                {"type": "surfing", "place": "pool", "hours": 3},
                {"type": "surf", "place_mention": "the river wave", "place_kind": "other"},
                {"type": "surf"},
            ]
        },
    }


def test_where_counts_spots_and_rolls_up_the_place_tree() -> None:
    graph, places = _surf_graph(), _surf_places()
    by_node = disciplines.where(graph, _surf_days(), places)

    surf = by_node["surf"]
    assert surf.spots["lakey-peak"].sessions == 2 and surf.spots["lakey-peak"].hours == 2
    assert surf.spots["lakey"].sessions == 1  # linked to the town only
    assert surf.spots["?the river wave"].sessions == 1
    assert surf.spots["?"].sessions == 1
    # A spot counts toward its town, region and country, each once.
    areas = {k: t.sessions for k, t in surf.areas.items()}
    assert areas == {
        "lakey": 3,
        "sumbawa": 3,
        "uluwatu": 1,
        "bali": 1,
        "indonesia": 4,
        "waco": 1,
        "texas": 1,
        "usa": 1,
    }
    assert surf.pool.sessions == 1 and surf.pool.last == date(2026, 11, 2)
    # Disciplines roll up: Water has both sports' places; non-sports are skipped.
    water = by_node["water"]
    assert water.areas["uluwatu"].sessions == 2
    assert water.areas["bali"].sessions == 2
    assert "travel" not in by_node


def test_where_data_and_line_rank_by_sessions() -> None:
    graph, places = _surf_graph(), _surf_places()
    surf = disciplines.where(graph, _surf_days(), places)["surf"]

    data = disciplines.where_data(surf, places)
    first = data["spots"][0]
    assert first["id"] == "lakey-peak" and first["sessions"] == 2
    assert first["within"] == "Lakey, Sumbawa" and first["last"] == "2026-09-26"
    names = [s["name"] for s in data["spots"]]
    assert "Lakey (spot not said)" in names
    assert 'spot unknown ("the river wave")' in names and "spot unknown" in names
    pool = next(s for s in data["spots"] if s["id"] == "pool")
    assert pool["kind"] == "wave_pool" and pool["hours"] == 3
    assert data["pool"] == 1 and data["unknown"] == 2
    levels = {a["id"]: a["level"] for a in data["areas"]}
    assert levels["sumbawa"] == "region" and levels["lakey"] == "town"
    assert levels["usa"] == "country"
    assert disciplines.where_data(None, places)["spots"] == []

    twin = PlaceIndex(
        [*places.nodes, {"id": "lakey-2", "name": "Lakey", "kind": "town", "parent": "bali"}]
    )
    assert disciplines.place_name("lakey", twin) == "Lakey (Sumbawa)"
    assert disciplines.spot_name("lakey", twin) == "Lakey (Sumbawa) (spot not said)"
    assert disciplines.place_name("lakey", places) == "Lakey"

    line = disciplines.where_line(surf, places, limit=2)
    assert line.startswith("Lakey Peak 2, ")
    assert "+4 more" in line
    assert "regions: Sumbawa 3, " in line and "Bali 1" in line and "Texas 1" in line
    assert line.endswith("wave pool 1")


def test_describe_and_graph_data_carry_where() -> None:
    graph, places = _surf_graph(), _surf_places()
    days = _surf_days()
    all_time = disciplines.totals(graph, days)
    by_node = disciplines.where(graph, days, places)

    text = disciplines.describe(graph, [], {}, all_time, 30, by_node, places)
    assert "Where, all time" in text
    assert "- Surfing 7 — Lakey Peak 2" in text
    assert "Water" not in text.split("Where, all time")[1]  # groupings left out
    assert "Where" not in disciplines.describe(graph, [], {}, all_time, 30)

    nodes = {n["id"]: n for n in disciplines.graph_data(graph, [], {}, all_time, by_node, places)}
    assert nodes["surf"]["where"]["pool"] == 1
    assert "where" not in disciplines.graph_data(graph, [], {}, all_time)[0]


def test_where_snapshot_reads_places_from_the_vault(vault: Vault) -> None:
    vault.write_place_files(
        {"_world": [{"id": "usa", "name": "United States", "kind": "country"}]},
        {"_world"},
        "places",
    )
    vault.write_journal_entry(
        datetime(2026, 10, 1, 9, 0),
        {"activities": [{"type": "surf", "place": "usa"}]},
        "Surfed.",
        "Surf.",
    )
    places, by_node = disciplines.where_snapshot(vault, date(2026, 10, 2), _surf_graph())
    assert places.get("usa") is not None
    assert by_node["surf"].areas["usa"].sessions == 1


def test_each_timed_session_counts_once() -> None:
    graph = Graph({**_graph().nodes, "surf": {"name": "Surfing", "parents": ["water"]}})
    three = [{"at": "16:00"}, {"at": "18:00"}, {"at": "19:00"}]
    days = {
        date(2026, 11, 2): {"activities": [{"type": "surf", "hours": 3, "sessions": three}]},
        date(2026, 11, 3): {"activities": [{"type": "surf", "hours": 2, "sessions": []}]},
    }
    by_node = disciplines.totals(graph, days)
    assert by_node["surf"].sessions == 4 and by_node["water"].sessions == 4
    assert by_node["surf"].hours == 5
