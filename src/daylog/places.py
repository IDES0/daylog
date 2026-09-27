"""The places tree: country -> region -> town -> spot, as plain data.

Pure logic — vault.py loads and saves the nodes. Every node is a mapping
with at least `id`, `name`, `kind`, and (for everything but a country) a
`parent` id. Geography is never inferred from a name: a node's region is
whatever its parent chain says it is. That's the whole point — "Airport
Rights" can't drift into Lombok when its parent is `bukit` under `bali`.

Nothing that can be derived is stored: whether a place was visited, and
when, comes from the journal and location.yaml (see `trail`), never a flag.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any

KINDS = (
    "country",
    "region",
    "park",
    "town",
    "surf_spot",
    "wind_spot",
    "dive_site",
    "hike",
    "viewpoint",
    "beach",
    "food",
    "stay",
    "venue",
    "transit",
    "other",
)

# Kinds that are areas other places sit inside, as opposed to a spot you go to.
AREA_KINDS = ("country", "region", "park", "town")

# Kinds whose per-place detail is worth sending to a model only for the
# region the user is in — there will be hundreds of warungs, and a food
# place three islands away is noise for resolving today's transcript.
LOCAL_ONLY_KINDS = ("food", "stay", "venue")


def slugify(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-") or "place"


def _norm(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", text.lower()).strip()


@dataclass
class PlaceIndex:
    """Lookup structure over a flat list of place nodes."""

    nodes: list[Any]

    def __post_init__(self) -> None:
        self.by_id: dict[str, Any] = {}
        self.children: dict[str, list[str]] = {}
        for node in self.nodes:
            node_id = node.get("id")
            if not node_id:
                continue
            self.by_id[node_id] = node
        for node_id, node in self.by_id.items():
            parent = node.get("parent")
            if parent:
                self.children.setdefault(parent, []).append(node_id)

    def get(self, place_id: str | None) -> Any | None:
        return self.by_id.get(place_id) if place_id else None

    def ancestors(self, place_id: str) -> list[Any]:
        """Parent first, up to the root. Stops on a cycle or a dangling parent."""
        chain: list[Any] = []
        seen = {place_id}
        node = self.by_id.get(place_id)
        while node is not None:
            parent_id = node.get("parent")
            if not parent_id or parent_id in seen:
                break
            seen.add(parent_id)
            node = self.by_id.get(parent_id)
            if node is not None:
                chain.append(node)
        return chain

    def region_of(self, place_id: str | None) -> Any | None:
        """The nearest region/park at or above `place_id` — the unit the user thinks in."""
        node = self.get(place_id)
        if node is None:
            return None
        for candidate in [node, *self.ancestors(place_id or "")]:
            if candidate.get("kind") in ("region", "park"):
                return candidate
        return None

    def country_of(self, place_id: str | None) -> Any | None:
        node = self.get(place_id)
        if node is None:
            return None
        for candidate in [node, *self.ancestors(place_id or "")]:
            if candidate.get("kind") == "country":
                return candidate
        return None

    def path_name(self, place_id: str) -> str:
        """'Lakey Peak, Lakey, Sumbawa, Indonesia' — unambiguous in any prompt."""
        node = self.by_id.get(place_id)
        if node is None:
            return place_id
        names = [str(node.get("name", place_id))]
        names += [str(a.get("name", a.get("id"))) for a in self.ancestors(place_id)]
        return ", ".join(names)

    def descendants(self, place_id: str, kinds: Iterable[str] | None = None) -> list[Any]:
        wanted = set(kinds) if kinds is not None else None
        out: list[Any] = []
        stack = list(self.children.get(place_id, []))
        seen: set[str] = set()
        while stack:
            child_id = stack.pop(0)
            if child_id in seen:
                continue
            seen.add(child_id)
            child = self.by_id[child_id]
            if wanted is None or child.get("kind") in wanted:
                out.append(child)
            stack.extend(self.children.get(child_id, []))
        return out

    def names_of(self, node: Any) -> list[str]:
        return [str(node.get("name", ""))] + [str(a) for a in node.get("aliases") or []]

    def resolve(self, text: str, within: str | None = None) -> list[str]:
        """Place ids whose name or alias matches `text`, best first.

        Exact (normalized) name/alias matches win; `within` (a region/town
        id) breaks ties in favor of places inside it — "Kuta" means the
        one on the island the user is on, not the more famous one.
        """
        target = _norm(text)
        if not target:
            return []
        exact: list[str] = []
        for node_id, node in self.by_id.items():
            if any(_norm(n) == target for n in self.names_of(node) if n):
                exact.append(node_id)
        if within and len(exact) > 1:
            inside = {within} | {d["id"] for d in self.descendants(within)}
            exact.sort(key=lambda i: i not in inside)
        return exact

    def resolve_location(self, location_name: str) -> Any | None:
        """The most specific node matching a 'Town, Region, Country' location string.

        Tries each comma part, most specific first, preferring a match that
        sits under a later part (so 'Kuta, Lombok, ID' finds the Kuta whose
        ancestors include Lombok). A bare country ('Indonesia') never
        matches — that would put the user in whichever region came first.
        """
        parts = [p.strip() for p in location_name.split(",") if p.strip()]
        for i, part in enumerate(parts):
            candidates = [c for c in self.resolve(part) if self.by_id[c].get("kind") != "country"]
            if not candidates:
                continue
            context = {_norm(p) for p in parts[i + 1 :]}

            def score(candidate_id: str, context: set[str] = context) -> int:
                # A region/town above the candidate named in the location
                # string is strong evidence; a shared country is only a
                # tiebreaker — every Kuta is in Indonesia.
                total = 0
                for ancestor in self.ancestors(candidate_id):
                    names = {_norm(n) for n in self.names_of(ancestor) if n}
                    if names & context:
                        total += 1 if ancestor.get("kind") == "country" else 10
                return total

            ranked = sorted(candidates, key=score, reverse=True)
            if len(ranked) == 1 or score(ranked[0]) > score(ranked[1]):
                return self.by_id[ranked[0]]
        return None

    def current_place(self, location_entry: Any | None) -> Any | None:
        """The node for a location.yaml entry: its `place_id` if set, else by name."""
        if not location_entry:
            return None
        node = self.get(location_entry.get("place_id"))
        if node is not None:
            return node
        return self.resolve_location(str(location_entry.get("place", "")))

    def forecast_spots(self, place: Any | None) -> list[Any]:
        """Surf/wind spots with coordinates in the same region as `place`."""
        if place is None:
            return []
        region = self.region_of(place["id"]) or place
        return [
            s
            for s in self.descendants(region["id"], ("surf_spot", "wind_spot"))
            if s.get("lat") is not None and s.get("lon") is not None
        ]


def new_node_id(index: PlaceIndex, name: str, parent_id: str | None) -> str:
    """A readable, unique id — the parent's id is appended only on a collision.

    "Kuta" under Lombok becomes `kuta`; the second Kuta, under Bali,
    becomes `kuta-bali`, never a silent overwrite.
    """
    base = slugify(name)
    if base not in index.by_id:
        return base
    if parent_id:
        region = index.region_of(parent_id)
        suffix = slugify(str(region.get("name"))) if region else slugify(parent_id)
        candidate = f"{base}-{suffix}"
        if candidate not in index.by_id:
            return candidate
    n = 2
    while f"{base}-{n}" in index.by_id:
        n += 1
    return f"{base}-{n}"


def file_for(index: PlaceIndex, parent_id: str | None) -> str:
    """Which places/<file>.yaml a new node under `parent_id` belongs in.

    One file per region keeps files small and diffs readable; countries
    and anything not under a region live in `_world.yaml`.
    """
    region = index.region_of(parent_id) if parent_id else None
    return str(region["id"]) if region else "_world"


def _describe_node(index: PlaceIndex, node: Any, detail: bool) -> str:
    kind = node.get("kind", "?")
    aliases = node.get("aliases") or []
    aka = f" aka {', '.join(str(a) for a in aliases)}" if aliases else ""
    line = f"- {node['id']}: {node.get('name', node['id'])} ({kind}){aka}"
    if not detail:
        return line
    facts = node.get("facts") or {}
    fact_bits = [f"{k} {v}" for k, v in facts.items() if v]
    if fact_bits:
        line += f" — {'; '.join(fact_bits)}"
    return line


def outline(index: PlaceIndex, focus: Any | None = None) -> str:
    """The tree as an indented list, for grounding a model.

    Everything but local-only kinds is always included (a day trip to
    another island is exactly when name grounding matters). Food/stay/
    venue nodes are included only inside the focus region.
    """
    focus_region = index.region_of(focus["id"]) if focus is not None else None
    focus_ids: set[str] = set()
    if focus_region is not None:
        focus_ids = {focus_region["id"]} | {d["id"] for d in index.descendants(focus_region["id"])}

    lines: list[str] = []

    def walk(node_id: str, depth: int) -> None:
        node = index.by_id[node_id]
        if node.get("kind") in LOCAL_ONLY_KINDS and node_id not in focus_ids:
            return
        lines.append("  " * depth + _describe_node(index, node, detail=node_id in focus_ids))
        for child_id in sorted(index.children.get(node_id, [])):
            walk(child_id, depth + 1)

    roots = [
        i for i, n in index.by_id.items() if not n.get("parent") or n["parent"] not in index.by_id
    ]
    for root_id in sorted(roots):
        walk(root_id, 0)
    return "\n".join(lines) if lines else "(no places yet)"


def describe_for_brief(index: PlaceIndex, current: Any | None) -> str:
    """Region-level knowledge for the brief: every region with its notes and
    checklist, the current one tagged and expanded with its spots."""
    current_region = index.region_of(current["id"]) if current is not None else None
    lines: list[str] = []
    regions = [n for n in index.by_id.values() if n.get("kind") in ("region", "park")]
    for region in regions:
        is_current = current_region is not None and region["id"] == current_region["id"]
        country = index.country_of(region["id"])
        where = f", {country.get('name')}" if country else ""
        tag = " [current]" if is_current else ""
        activities = ", ".join(region.get("activities") or []) or "unspecified"
        notes = f" — {region['notes']}" if region.get("notes") else ""
        lines.append(f"- {region.get('name')}{where}{tag}: {activities}{notes}")
        for item in region.get("checklist") or []:
            status = item.get("status", "todo")
            if status == "done":
                continue
            item_notes = f" — {item['notes']}" if item.get("notes") else ""
            lines.append(f"    - [{status}] {item.get('item', '?')}{item_notes}")
        spot_kinds = ("surf_spot", "wind_spot", "dive_site", "hike", "viewpoint", "beach")
        for spot in index.descendants(region["id"], spot_kinds):
            if not is_current and spot.get("kind") not in ("surf_spot", "wind_spot"):
                continue
            facts = spot.get("facts") or {}
            fact_bits = "; ".join(f"{k} {v}" for k, v in facts.items() if v)
            fact_str = f" ({fact_bits})" if fact_bits else ""
            spot_notes = f" — {spot['notes']}" if spot.get("notes") else ""
            lines.append(f"    - {spot.get('name')} [{spot.get('kind')}]{fact_str}{spot_notes}")
    return "\n".join(lines) if lines else "(none curated yet)"
