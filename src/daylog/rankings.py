"""Beli-style personal rankings: a tier, then a few head-to-head comparisons.

Pure logic. rankings.yaml holds one ordered list per category and tier,
best first:

    food:
      liked: [artisan-uluwatu, this-is-bali]
      fine: [finns-beach-club]
      disliked: []

The order is the source of truth; a 0-10 score is derived from position
(like Beli, a score is only meaningful relative to everything else you've
ranked). A new place is placed by binary insertion within its tier, so
ranking the 30th restaurant takes about five taps, not thirty.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

# category -> the place kinds ranked in it
CATEGORIES: dict[str, tuple[str, ...]] = {
    "food": ("food",),
    "surf": ("surf_spot",),
    "dive": ("dive_site",),
    "stay": ("stay",),
    "outdoors": ("hike", "viewpoint", "beach"),
    "nightlife": ("venue",),
    "flying": ("fly_site",),
}

TIERS = ("liked", "fine", "disliked")

# Score band per tier, as (low, high); position within the tier interpolates.
_BANDS = {"liked": (6.7, 10.0), "fine": (3.4, 6.6), "disliked": (0.0, 3.3)}


def category_for(kind: str | None) -> str | None:
    for category, kinds in CATEGORIES.items():
        if kind in kinds:
            return category
    return None


def tier_list(rankings: Any, category: str, tier: str) -> list[str]:
    return list((rankings.get(category) or {}).get(tier) or [])


def ranked(rankings: Any, category: str) -> list[tuple[str, str]]:
    """(place_id, tier) best to worst across all tiers."""
    return [(pid, tier) for tier in TIERS for pid in tier_list(rankings, category, tier)]


def position(rankings: Any, category: str, place_id: str) -> tuple[str, int] | None:
    for tier in TIERS:
        items = tier_list(rankings, category, tier)
        if place_id in items:
            return tier, items.index(place_id)
    return None


def score(rankings: Any, category: str, place_id: str) -> float | None:
    found = position(rankings, category, place_id)
    if found is None:
        return None
    tier, idx = found
    low, high = _BANDS[tier]
    count = len(tier_list(rankings, category, tier))
    if count <= 1:
        return round(high if tier != "disliked" else (low + high) / 2, 1)
    return round(high - (high - low) * idx / (count - 1), 1)


def describe(rankings: Any, category: str, place_id: str) -> str | None:
    """'#2 of 9 food · 9.2' for a place card, or None if unranked."""
    s = score(rankings, category, place_id)
    if s is None:
        return None
    order = [pid for pid, _ in ranked(rankings, category)]
    return f"#{order.index(place_id) + 1} of {len(order)} {category} · {s:.1f}"


@dataclass
class Insertion:
    """An in-progress binary insertion of `place_id` into one tier."""

    category: str
    tier: str
    place_id: str
    lo: int
    hi: int


def start(rankings: Any, category: str, place_id: str, tier: str) -> Insertion:
    """Begin placing `place_id` in `tier`. Re-ranking a place removes it first,
    so its old position never biases the comparisons."""
    others = [p for p in tier_list(rankings, category, tier) if p != place_id]
    return Insertion(category=category, tier=tier, place_id=place_id, lo=0, hi=len(others))


def _others(rankings: Any, ins: Insertion) -> list[str]:
    return [p for p in tier_list(rankings, ins.category, ins.tier) if p != ins.place_id]


def next_opponent(rankings: Any, ins: Insertion) -> str | None:
    """The place to compare against next, or None when the slot is found."""
    if ins.lo >= ins.hi:
        return None
    return _others(rankings, ins)[(ins.lo + ins.hi) // 2]


def answer(ins: Insertion, new_is_better: bool) -> Insertion:
    mid = (ins.lo + ins.hi) // 2
    if new_is_better:
        return Insertion(ins.category, ins.tier, ins.place_id, ins.lo, mid)
    return Insertion(ins.category, ins.tier, ins.place_id, mid + 1, ins.hi)


def settle(ins: Insertion) -> Insertion:
    """'Too close to call' — stop comparing and take the current midpoint."""
    mid = (ins.lo + ins.hi) // 2
    return Insertion(ins.category, ins.tier, ins.place_id, mid, mid)


def finish(rankings: Any, ins: Insertion) -> Any:
    """Insert at the found slot, removing any previous placement. Mutates and
    returns `rankings` (the live ruamel structure, so comments survive)."""
    for tier in TIERS:
        existing = (rankings.get(ins.category) or {}).get(tier)
        if existing and ins.place_id in existing:
            existing.remove(ins.place_id)
    category = rankings.get(ins.category)
    if category is None:
        category = {}
        rankings[ins.category] = category
    items = category.get(ins.tier)
    if items is None:
        items = []
        category[ins.tier] = items
    items.insert(min(ins.lo, len(items)), ins.place_id)
    return rankings


def unranked(rankings: Any, candidates: list[tuple[str, str | None]]) -> list[tuple[str, str]]:
    """(place_id, category) for candidate (place_id, kind) pairs worth asking about:
    rankable kind, not yet ranked, deduped, in the order given."""
    out: list[tuple[str, str]] = []
    for place_id, kind in candidates:
        category = category_for(kind)
        if category is None or position(rankings, category, place_id) is not None:
            continue
        if (place_id, category) not in out:
            out.append((place_id, category))
    return out
