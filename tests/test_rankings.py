from __future__ import annotations

from typing import Any

from daylog import rankings
from daylog.places import PlaceIndex
from daylog.rank_flow import format_rankings, rank_candidates

from .place_fixtures import tree


def _insert(current: dict[str, Any], place_id: str, tier: str, better_than: set[str]) -> None:
    """Run a full insertion, answering each comparison from `better_than`."""
    ins = rankings.start(current, "food", place_id, tier)
    while (opponent := rankings.next_opponent(current, ins)) is not None:
        ins = rankings.answer(ins, new_is_better=opponent in better_than)
    rankings.finish(current, ins)


def test_binary_insertion_finds_the_right_slot() -> None:
    current: dict[str, Any] = {"food": {"liked": ["a", "b", "c", "d"]}}
    _insert(current, "x", "liked", better_than={"c", "d"})
    assert current["food"]["liked"] == ["a", "b", "x", "c", "d"]


def test_insertion_into_an_empty_tier_needs_no_comparisons() -> None:
    current: dict[str, Any] = {}
    ins = rankings.start(current, "food", "x", "fine")
    assert rankings.next_opponent(current, ins) is None
    rankings.finish(current, ins)
    assert current == {"food": {"fine": ["x"]}}


def test_reranking_moves_rather_than_duplicates() -> None:
    current: dict[str, Any] = {"food": {"liked": ["a", "x"], "fine": ["b"]}}
    _insert(current, "x", "fine", better_than={"b"})
    assert current["food"] == {"liked": ["a"], "fine": ["x", "b"]}


def test_settle_stops_at_the_midpoint() -> None:
    current: dict[str, Any] = {"food": {"liked": ["a", "b", "c"]}}
    ins = rankings.settle(rankings.start(current, "food", "x", "liked"))
    assert rankings.next_opponent(current, ins) is None
    rankings.finish(current, ins)
    assert current["food"]["liked"] == ["a", "x", "b", "c"]


def test_scores_follow_tier_bands_and_position() -> None:
    current = {"food": {"liked": ["a", "b", "c"], "fine": ["d"], "disliked": ["e"]}}
    assert rankings.score(current, "food", "a") == 10.0
    assert rankings.score(current, "food", "c") == 6.7
    assert rankings.score(current, "food", "d") == 6.6
    assert rankings.score(current, "food", "e") is not None
    assert rankings.score(current, "food", "e") < 3.4  # type: ignore[operator]
    assert rankings.score(current, "food", "zzz") is None
    assert rankings.describe(current, "food", "b") == "#2 of 5 food · 8.3"


def test_unranked_filters_kinds_and_already_ranked() -> None:
    current = {"food": {"liked": ["ramen-otaku"]}}
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
    text = format_rankings({"food": {"liked": ["ramen-otaku"]}}, PlaceIndex(tree()), "food")
    assert text == "FOOD\n1. Ramen Otaku — 10.0 · Lombok"
