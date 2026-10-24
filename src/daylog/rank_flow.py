"""Rankings' vault side: which places in an entry can be ranked, and the list as text.

Ranking itself is a binary insertion (rankings.py): a tier, then one
"which was better?" question at a time until the slot is found, then
rankings.yaml is written once.
"""

from __future__ import annotations

import logging
from typing import Any

from daylog import rankings
from daylog.places import PlaceIndex

logger = logging.getLogger(__name__)

RANKINGS_FILE = "rankings.yaml"
# Don't turn one voice note into a questionnaire.
MAX_PROMPTS_PER_ENTRY = 2


def _name(index: PlaceIndex, place_id: str) -> str:
    node = index.get(place_id)
    return str(node.get("name")) if node else place_id


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


def format_rankings(current: Any, index: PlaceIndex, category: str | None) -> str:
    categories = [category] if category else list(rankings.CATEGORIES)
    blocks = []
    for cat in categories:
        order = rankings.ranked(current, cat)
        if not order:
            continue
        lines = [f"{cat.upper()}"]
        for i, (place_id, _tier) in enumerate(order, start=1):
            score = rankings.score(current, cat, place_id)
            region = index.region_of(place_id)
            where = f" · {region.get('name')}" if region else ""
            lines.append(f"{i}. {_name(index, place_id)} — {score:.1f}{where}")
        blocks.append("\n".join(lines))
    return "\n\n".join(blocks) if blocks else "Nothing ranked yet."
