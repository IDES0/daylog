# ruff: noqa: F811  (the `app` fixture is imported, then named as a parameter)
"""The web ranking flow: head-to-heads, manual moves, unranking, rank cards."""

from __future__ import annotations

from typing import Any

import pytest

from daylog import extract, rankings
from daylog.vault import Vault

from .place_fixtures import tree
from .test_webapp import App, _json, _logged, app  # noqa: F401  (app is a fixture)

FOOD = ["a", "b", "c", "d"]


def _places(vault: Vault, ranked: dict[str, Any] | None = None) -> None:
    extra = [{"id": p, "name": p.upper(), "kind": "food", "parent": "kuta"} for p in FOOD]
    extra.append({"id": "x", "name": "Warung X", "kind": "food", "parent": "kuta"})
    vault.write_place_files({"indonesia": tree() + extra}, {"indonesia"}, "seed places")
    if ranked is not None:
        vault.write_yaml(rankings.RANKINGS_FILE, ranked, "seed rankings")


def _rank(app: App, place: str, better_than: set[str]) -> tuple[Any, int]:
    """Run the whole flow through the API, answering from `better_than`."""
    status, step = _json(app, "POST", "/api/rankings/session", {"place": place})
    asked = 0
    while status == 200 and not step["done"]:
        asked += 1
        answer = "better" if step["other"]["id"] in better_than else "worse"
        status, step = _json(
            app, "POST", "/api/rankings/session/next", {**step["state"], "answer": answer}
        )
    assert status == 200, step
    return step, asked


# -- pure ------------------------------------------------------------------------------


def test_restore_checks_bounds_against_the_list() -> None:
    current: dict[str, Any] = {"food": ["a", "b"]}
    assert rankings.restore(current, "food", "x", 0, 2).hi == 2
    with pytest.raises(ValueError):
        rankings.restore(current, "food", "x", 0, 5)


def test_comparisons_left_is_a_binary_search() -> None:
    ins = rankings.Insertion("food", "x", 0, 30)
    assert rankings.comparisons_left(ins) == 5
    assert rankings.comparisons_left(rankings.Insertion("food", "x", 3, 3)) == 0


# -- API -------------------------------------------------------------------------------


def test_a_full_insertion_lands_at_the_right_index(app: App, vault: Vault) -> None:
    _places(vault, {"food": list(FOOD)})
    done, asked = _rank(app, "x", better_than={"c", "d"})
    assert done["done"] and done["index"] == 2 and "tier" not in done
    assert asked <= 3
    assert vault.read_yaml(rankings.RANKINGS_FILE, {})["food"] == ["a", "b", "x", "c", "d"]
    assert done["line"] == "#3 of 5 food · 5.5"


def test_old_tiered_file_ranks_across_all_of_it(app: App, vault: Vault) -> None:
    _places(vault, {"food": {"liked": ["a", "b"], "fine": ["c"], "disliked": ["d"]}})
    done, _ = _rank(app, "x", better_than={"d"})
    assert done["index"] == 3
    assert vault.read_yaml(rankings.RANKINGS_FILE, {})["food"] == ["a", "b", "c", "x", "d"]


def test_an_empty_list_saves_at_once(app: App, vault: Vault) -> None:
    _places(vault)
    done, asked = _rank(app, "x", set())
    assert asked == 0 and done["done"] and done["index"] == 0
    assert vault.read_yaml(rankings.RANKINGS_FILE, {}) == {"food": ["x"]}


def test_tie_settles_and_bad_input_is_refused(app: App, vault: Vault) -> None:
    _places(vault, {"food": list(FOOD)})
    _, step = _json(app, "POST", "/api/rankings/session", {"place": "x"})
    assert not step["done"] and step["other"]["name"] == "C" and step["left"] == 3
    _, done = _json(app, "POST", "/api/rankings/session/next", {**step["state"], "answer": "tie"})
    assert done["done"] and done["index"] == 2

    def refused(path: str, body: dict[str, Any]) -> tuple[int, str]:
        status, out = _json(app, "POST", path, body)
        return status, out["error"]

    assert refused("/api/rankings/session", {"place": "nope"}) == (404, "not_found")
    assert refused("/api/rankings/session", {"place": "kuta"}) == (400, "not_rankable")
    stale = {"place": "x", "category": "food", "lo": 0, "hi": 40}
    assert refused("/api/rankings/session/next", {**stale, "answer": "better"}) == (409, "stale")
    # Writes need the CSRF header like everything else.
    status, _ = _json(app, "POST", "/api/rankings/move", {}, csrf=False)
    assert status == 403


def test_move_and_remove_persist(app: App, vault: Vault) -> None:
    _places(vault, {"food": ["a", "b", "c", "d"]})
    status, moved = _json(
        app, "POST", "/api/rankings/move", {"category": "food", "item": "c", "index": 0}
    )
    assert status == 200 and moved["index"] == 0
    status, moved = _json(
        app, "POST", "/api/rankings/move", {"category": "food", "item": "a", "index": 9}
    )
    assert status == 200 and moved["index"] == 3
    assert vault.read_yaml(rankings.RANKINGS_FILE, {})["food"] == ["c", "b", "d", "a"]
    status, gone = _json(app, "POST", "/api/rankings/remove", {"category": "food", "item": "b"})
    assert status == 200 and gone["message"] == "Unranked B."
    status, again = _json(app, "POST", "/api/rankings/remove", {"category": "food", "item": "b"})
    assert status == 404

    _, listed = _json(app, "GET", "/api/rankings?category=food")
    items = listed["lists"]["food"]
    assert [(i["id"], i["rank"], i["score"]) for i in items] == [
        ("c", 1, 10.0),
        ("d", 2, 5.5),
        ("a", 3, 1.0),
    ]
    assert items[0]["region"] == "Lombok" and "tiers" not in listed
    assert listed["counts"]["food"] == 3 and listed["counts"]["surf"] == 0


def test_a_note_offers_a_rank_card_that_starts_the_flow(
    app: App, vault: Vault, monkeypatch: pytest.MonkeyPatch
) -> None:
    _places(vault, {"food": ["a"]})
    monkeypatch.setattr(
        extract,
        "extract",
        lambda *a, **k: {
            "summary": "Lunch at Warung X.",
            "meals": [{"items": ["nasi"], "place": "x"}, {"items": ["ramen"], "place": "a"}],
        },
    )
    body = _logged(app, {"text": "lunch at warung x"})
    [card] = [c for c in body["cards"] if c["kind"] == "rank"]
    assert card["title"] == "Rank Warung X?"
    assert card["data"] == {"place": "x", "category": "food", "name": "Warung X"}
    assert [a["id"] for a in card["actions"]] == ["rank", "skip"]

    status, step = _json(app, "POST", "/api/rankings/session", {"place": "x", "card": card["id"]})
    assert status == 200 and not step["done"] and step["other"]["id"] == "a"
    # Taking the flow answers the card.
    assert all(c["id"] != card["id"] for c in _json(app, "GET", "/api/cards")[1]["cards"])


def test_a_rank_card_answered_directly_places_it_mid_list(
    app: App, vault: Vault, monkeypatch: pytest.MonkeyPatch
) -> None:
    _places(vault, {"food": ["a", "b"]})
    card = app.api._rank_card("x", "food", "Warung X")
    status, out = _json(app, "POST", f"/api/cards/{card.id}", {"action": "rank"})
    assert status == 200 and out["ok"]
    assert vault.read_yaml(rankings.RANKINGS_FILE, {})["food"] == ["a", "x", "b"]
