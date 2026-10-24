"""Personal rankings: one ordered list per category, placed by head-to-heads.

Pure logic. rankings.yaml holds one list per category, best first:

    food: [toshokan, uchiko, artisan-uluwatu]

The order is the source of truth; a 1-10 score is derived from position, so
a score only means something relative to everything else in that list. A
new place is placed by binary insertion over the whole list, so ranking the
100th restaurant takes about seven taps, not a hundred.

Lists written before 2026-10-24 were split into liked/fine/disliked tiers;
they read as one list in that order, and the next write flattens them.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from daylog.places import PlaceIndex

# category -> the place kinds ranked in it
CATEGORIES: dict[str, tuple[str, ...]] = {
    "food": ("food",),
    "surf": ("surf_spot",),
    "dive": ("dive_site",),
    "outdoors": ("hike", "viewpoint", "beach"),
    "nightlife": ("venue",),
    "flying": ("fly_site",),
}

# The old tiered layout, best tier first (read only).
_OLD_TIERS = ("liked", "fine", "disliked")

TOP_SCORE = 10.0
BOTTOM_SCORE = 1.0


def category_for(kind: str | None) -> str | None:
    for category, kinds in CATEGORIES.items():
        if kind in kinds:
            return category
    return None


def ranked(rankings: Any, category: str) -> list[str]:
    """Place ids in `category`, best first."""
    value = (rankings or {}).get(category)
    if isinstance(value, dict):
        return [str(p) for tier in _OLD_TIERS for p in (value.get(tier) or [])]
    return [str(p) for p in (value or [])]


def items(rankings: Any, category: str) -> Any:
    """The live list for `category` inside `rankings`, created or flattened
    from the old tiers as needed, for in-place edits (ruamel keeps comments)."""
    value = rankings.get(category)
    if isinstance(value, list):
        return value
    rankings[category] = ranked(rankings, category)
    return rankings[category]


def position(rankings: Any, category: str, place_id: str) -> int | None:
    order = ranked(rankings, category)
    return order.index(place_id) if place_id in order else None


def score(rankings: Any, category: str, place_id: str) -> float | None:
    """10 for the best, 1 for the worst, evenly spaced between."""
    idx = position(rankings, category, place_id)
    if idx is None:
        return None
    count = len(ranked(rankings, category))
    if count <= 1:
        return TOP_SCORE
    return round(TOP_SCORE - (TOP_SCORE - BOTTOM_SCORE) * idx / (count - 1), 1)


def describe(rankings: Any, category: str, place_id: str) -> str | None:
    """'#2 of 9 food · 8.9' for a place card, or None if unranked."""
    idx = position(rankings, category, place_id)
    if idx is None:
        return None
    count = len(ranked(rankings, category))
    return f"#{idx + 1} of {count} {category} · {score(rankings, category, place_id):.1f}"


@dataclass
class Insertion:
    """An in-progress binary insertion of `place_id` into a category's list."""

    category: str
    place_id: str
    lo: int
    hi: int


def _others(rankings: Any, category: str, place_id: str) -> list[str]:
    return [p for p in ranked(rankings, category) if p != place_id]


def start(rankings: Any, category: str, place_id: str) -> Insertion:
    """Begin placing `place_id`. Re-ranking a place leaves it out of the
    comparisons, so its old position never biases them."""
    return Insertion(category, place_id, 0, len(_others(rankings, category, place_id)))


def next_opponent(rankings: Any, ins: Insertion) -> str | None:
    """The place to compare against next, or None when the slot is found."""
    if ins.lo >= ins.hi:
        return None
    return _others(rankings, ins.category, ins.place_id)[(ins.lo + ins.hi) // 2]


def answer(ins: Insertion, new_is_better: bool) -> Insertion:
    mid = (ins.lo + ins.hi) // 2
    if new_is_better:
        return Insertion(ins.category, ins.place_id, ins.lo, mid)
    return Insertion(ins.category, ins.place_id, mid + 1, ins.hi)


def settle(ins: Insertion) -> Insertion:
    """'Too close to call' — stop comparing and take the current midpoint."""
    mid = (ins.lo + ins.hi) // 2
    return Insertion(ins.category, ins.place_id, mid, mid)


def finish(rankings: Any, ins: Insertion) -> Any:
    """Insert at the found slot, removing any previous placement. Mutates and
    returns `rankings` (the live ruamel structure, so comments survive)."""
    lst = items(rankings, ins.category)
    if ins.place_id in lst:
        lst.remove(ins.place_id)
    lst.insert(min(ins.lo, len(lst)), ins.place_id)
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


# -- used by the web app (webapp/api.py) ---------------------------------------

RANKINGS_FILE = "rankings.yaml"
# Don't turn one note into a questionnaire.
MAX_PROMPTS_PER_ENTRY = 2


def rank_candidates(facts: dict[str, Any], index: PlaceIndex) -> list[tuple[str, str | None]]:
    """(place_id, kind) for every linked place in an entry's activities and meals."""
    out: list[tuple[str, str | None]] = []
    for field_name in ("meals", "activities"):
        for item in facts.get(field_name) or []:
            if not isinstance(item, dict):
                continue
            place_id = item.get("place")
            node = index.get(place_id)
            if node is not None:
                out.append((str(place_id), node.get("kind")))
    return out


def to_prompt(rankings: Any, facts: dict[str, Any], index: PlaceIndex) -> list[tuple[str, str]]:
    """(place_id, category) for the unranked places in a logged entry worth a
    "rank it?" prompt, at most MAX_PROMPTS_PER_ENTRY."""
    return unranked(rankings, rank_candidates(facts, index))[:MAX_PROMPTS_PER_ENTRY]


def restore(rankings: Any, category: str, place_id: str, lo: int, hi: int) -> Insertion:
    """An insertion a client carried between comparisons (the web app holds the
    bounds, not the server). ValueError if they no longer fit the list."""
    if not 0 <= lo <= hi <= len(_others(rankings, category, place_id)):
        raise ValueError("bounds no longer fit the list")
    return Insertion(category, place_id, lo, hi)


def comparisons_left(ins: Insertion) -> int:
    """At most this many more questions (a binary search over hi - lo slots)."""
    return (ins.hi - ins.lo).bit_length()


def remove(rankings: Any, category: str, place_id: str) -> bool:
    """Unrank a place in one category. Mutates `rankings`; False if it wasn't ranked."""
    if place_id not in ranked(rankings, category):
        return False
    items(rankings, category).remove(place_id)
    return True


def move(rankings: Any, category: str, place_id: str, index: int) -> int:
    """A manual reorder: put `place_id` at `index` (0 = best), taking it out of
    wherever it was. `index` counts the list without the place, so it is the
    slot the place ends up in. Mutates `rankings`; returns that slot."""
    remove(rankings, category, place_id)
    slot = max(0, min(index, len(ranked(rankings, category))))
    finish(rankings, Insertion(category, place_id, slot, slot))
    return slot
