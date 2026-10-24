from __future__ import annotations

from typing import Any

from daylog import rankings
from daylog.places import PlaceIndex
from daylog.rank_flow import format_rankings, rank_candidates

from .place_fixtures import tree


def _insert(current: dict[str, Any], place_id: str, better_than: set[str]) -> None:
    """Run a full insertion, answering each comparison from `better_than`."""
    ins = rankings.start(current, "food", place_id)
    while (opponent := rankings.next_opponent(current, ins)) is not None:
        ins = rankings.answer(ins, new_is_better=opponent in better_than)
    rankings.finish(current, ins)


def test_binary_insertion_finds_the_right_slot() -> None:
    current: dict[str, Any] = {"food": ["a", "b", "c", "d"]}
    _insert(current, "x", better_than={"c", "d"})
    assert current["food"] == ["a", "b", "x", "c", "d"]


def test_insertion_into_an_empty_list_needs_no_comparisons() -> None:
    current: dict[str, Any] = {}
    ins = rankings.start(current, "food", "x")
    assert rankings.next_opponent(current, ins) is None
    rankings.finish(current, ins)
    assert current == {"food": ["x"]}


def test_reranking_moves_rather_than_duplicates() -> None:
    current: dict[str, Any] = {"food": ["a", "x", "b"]}
    _insert(current, "x", better_than={"a", "b"})
    assert current["food"] == ["x", "a", "b"]


def test_settle_stops_at_the_midpoint() -> None:
    current: dict[str, Any] = {"food": ["a", "b", "c"]}
    ins = rankings.settle(rankings.start(current, "food", "x"))
    assert rankings.next_opponent(current, ins) is None
    rankings.finish(current, ins)
    assert current["food"] == ["a", "x", "b", "c"]


def test_scores_are_relative_to_the_whole_list() -> None:
    current = {"food": ["a", "b", "c", "d"]}
    assert rankings.score(current, "food", "a") == 10.0
    assert rankings.score(current, "food", "b") == 7.0
    assert rankings.score(current, "food", "d") == 1.0
    assert rankings.score({"food": ["solo"]}, "food", "solo") == 10.0
    assert rankings.score(current, "food", "zzz") is None
    assert rankings.describe(current, "food", "b") == "#2 of 4 food · 7.0"


def test_old_tiers_read_as_one_list_and_flatten_on_write() -> None:
    current: dict[str, Any] = {"food": {"liked": ["a", "b"], "fine": ["c"], "disliked": ["d"]}}
    assert rankings.ranked(current, "food") == ["a", "b", "c", "d"]
    assert rankings.score(current, "food", "d") == 1.0
    rankings.move(current, "food", "d", 1)
    assert current["food"] == ["a", "d", "b", "c"]


def test_move_and_remove() -> None:
    current: dict[str, Any] = {"food": ["a", "b", "c"]}
    assert rankings.move(current, "food", "c", 0) == 0
    assert current["food"] == ["c", "a", "b"]
    assert rankings.move(current, "food", "c", 99) == 2
    assert current["food"] == ["a", "b", "c"]
    assert rankings.remove(current, "food", "b") is True
    assert rankings.remove(current, "food", "b") is False
    assert current["food"] == ["a", "c"]


def test_unranked_filters_kinds_and_already_ranked() -> None:
    current = {"food": ["ramen-otaku"]}
    todo = rankings.unranked(
        current,
        [
            ("ramen-otaku", "food"),
            ("ekas-bay", "surf_spot"),
            ("kuta", "town"),
            ("ekas-bay", "surf_spot"),
        ],
    )
    assert todo == [("ekas-bay", "surf")]


def test_rank_candidates_reads_meals_and_activities() -> None:
    facts = {
        "activities": [{"type": "surf", "place": "ekas-bay"}, {"type": "walk"}],
        "meals": [{"items": ["ramen"], "place": "ramen-otaku"}, {"items": ["x"], "place": "nope"}],
    }
    assert rank_candidates(facts, PlaceIndex(tree())) == [
        ("ramen-otaku", "food"),
        ("ekas-bay", "surf_spot"),
    ]


def test_format_rankings_numbers_and_scores() -> None:
    text = format_rankings({"food": ["ramen-otaku"]}, PlaceIndex(tree()), "food")
    assert text == "FOOD\n1. Ramen Otaku — 10.0 · Lombok"
