"""Rankings' vault side: which places in an entry can be ranked, and the list as text.

Ranking itself is a binary insertion (rankings.py): a tier, then one
"which was better?" question at a time until the slot is found, then
rankings.yaml is written once. The web app drives it (webapp/api.py).
"""

from __future__ import annotations

import logging
from typing import Any

from daylog import rankings
from daylog.places import PlaceIndex
from daylog.rankings import MAX_PROMPTS_PER_ENTRY, RANKINGS_FILE, rank_candidates

logger = logging.getLogger(__name__)

__all__ = ["MAX_PROMPTS_PER_ENTRY", "RANKINGS_FILE", "format_rankings", "rank_candidates"]


def _name(index: PlaceIndex, place_id: str) -> str:
    node = index.get(place_id)
    return str(node.get("name")) if node else place_id


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
