"""The research agent: web search -> proposed places, with sources.

Claude (llm.MAIN_MODEL) with the server-side web_search tool and one
custom tool, `record_places`, that it calls once to hand back structured
findings. Everything it finds is a *proposal* — research_flow.py shows
each one as a confirm card before anything touches the places tree,
because inferred geography is exactly the class of error this exists to
fix. Pure apart from the API call: no vault access here.

Jobs (each is just a different prompt over the same loop):
- resolve: place names from journal entries that aren't in the tree yet
- arrive: the key spots of a region the user just arrived in
- trip: the stops of a multi-day trip, matched day by day to the journal
- dossier: a markdown research file for a place the user wants to go
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Any

import anthropic
from anthropic.types import MessageParam, ToolParam, WebSearchTool20260209Param

from daylog import llm
from daylog.places import KINDS, PlaceIndex, file_for, new_node_id, outline

logger = logging.getLogger(__name__)

PROMPT_PATH = Path(__file__).parent / "prompts" / "research.md"
DOSSIER_PROMPT_PATH = Path(__file__).parent / "prompts" / "dossier.md"
MAX_TURNS = 8

RECORD_PLACES_TOOL: ToolParam = {
    "name": "record_places",
    "description": "Hand back your findings. Call exactly once, at the end.",
    "input_schema": {
        "type": "object",
        "properties": {
            "matches": {
                "type": "array",
                "description": "Mentions that are a place already in Known places.",
                "items": {
                    "type": "object",
                    "properties": {
                        "mention": {"type": "string"},
                        "place_id": {"type": "string"},
                        "day": {
                            "type": "string",
                            "description": "Trip jobs: the ISO date of this stop.",
                        },
                    },
                    "required": ["mention", "place_id"],
                },
            },
            "new_places": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "ref": {
                            "type": "string",
                            "description": "Unique label, usable as another place's parent_ref.",
                        },
                        "mention": {
                            "type": "string",
                            "description": "The journal mention this resolves, verbatim, if any.",
                        },
                        "day": {
                            "type": "string",
                            "description": "Trip jobs: the ISO date this stop belongs to.",
                        },
                        "name": {"type": "string"},
                        "kind": {"type": "string", "enum": list(KINDS)},
                        "parent_id": {
                            "type": "string",
                            "description": "Id of an existing Known place this sits inside.",
                        },
                        "parent_ref": {
                            "type": "string",
                            "description": "Or: the ref of another new place in this list.",
                        },
                        "aliases": {"type": "array", "items": {"type": "string"}},
                        "lat": {"type": "number"},
                        "lon": {"type": "number"},
                        "confidence": {
                            "type": "string",
                            "enum": ["verified", "approximate", "inferred"],
                            "description": (
                                "verified: sources agree on what and where. approximate: "
                                "real place, rough position. inferred: best guess from context."
                            ),
                        },
                        "description": {
                            "type": "string",
                            "description": "Two or three sentences: what it is, why it matters.",
                        },
                        "facts": {
                            "type": "object",
                            "description": (
                                "Kind-specific short facts, string values. Surf: break, "
                                "swell_tide, level, hazards. Dive: depth, current, level, "
                                "highlights. Food: cuisine, price. Hike: duration, difficulty."
                            ),
                            "additionalProperties": {"type": "string"},
                        },
                        "sources": {"type": "array", "items": {"type": "string"}},
                    },
                    "required": ["ref", "name", "kind", "confidence", "description"],
                },
            },
            "unresolved": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {"mention": {"type": "string"}, "why": {"type": "string"}},
                    "required": ["mention"],
                },
            },
            "summary": {"type": "string", "description": "One or two sentences for the user."},
        },
        "required": ["new_places", "summary"],
    },
}


@dataclass
class Findings:
    matches: list[dict[str, Any]] = field(default_factory=list)
    new_places: list[dict[str, Any]] = field(default_factory=list)
    unresolved: list[dict[str, Any]] = field(default_factory=list)
    summary: str = ""
    cost: float = 0.0


def _web_search(max_uses: int) -> WebSearchTool20260209Param:
    return {"type": "web_search_20260209", "name": "web_search", "max_uses": max_uses}


def _parse(raw: dict[str, Any], index: PlaceIndex) -> Findings:
    """Keep only well-formed proposals: known kinds, real parents, no invented ids."""
    findings = Findings(summary=str(raw.get("summary", "")))
    for m in raw.get("matches") or []:
        if isinstance(m, dict) and m.get("place_id") in index.by_id and m.get("mention"):
            match = {"mention": str(m["mention"]), "place_id": m["place_id"]}
            if m.get("day"):
                match["day"] = str(m["day"])
            findings.matches.append(match)
    refs = {p.get("ref") for p in raw.get("new_places") or [] if isinstance(p, dict)}
    for p in raw.get("new_places") or []:
        if not isinstance(p, dict) or p.get("kind") not in KINDS or not p.get("name"):
            continue
        has_parent = p.get("parent_id") in index.by_id or p.get("parent_ref") in refs
        if not has_parent and p.get("kind") != "country":
            logger.info("dropping proposal %r: no valid parent", p.get("name"))
            continue
        findings.new_places.append(p)
    findings.unresolved = [u for u in raw.get("unresolved") or [] if isinstance(u, dict)]
    return findings


def run(
    job: str,
    index: PlaceIndex,
    focus: Any | None,
    *,
    max_searches: int = 8,
    budget: float | None = None,
    client: anthropic.Anthropic | None = None,
) -> Findings:
    """One research run. `job` is the job-specific instructions and data."""
    client = client or anthropic.Anthropic()
    cap = llm.research_run_budget() if budget is None else budget
    system = PROMPT_PATH.read_text(encoding="utf-8")
    messages: list[MessageParam] = [
        {
            "role": "user",
            "content": (
                f"Today: {date.today().isoformat()}\n\n"
                f"Known places (id: name (kind) aka aliases; indented under parent):\n"
                f"{outline(index, focus)}\n\n{job}"
            ),
        }
    ]
    tools: list[Any] = [_web_search(max_searches), RECORD_PLACES_TOOL]
    spent = 0.0
    force = False
    for _turn in range(MAX_TURNS):
        kwargs: dict[str, Any] = {}
        if force:
            kwargs["tool_choice"] = {"type": "tool", "name": "record_places"}
        response = client.messages.create(
            model=llm.MAIN_MODEL,
            max_tokens=16000,
            system=system,
            tools=tools,
            messages=messages,
            **kwargs,
        )
        spent += llm.record("research", llm.MAIN_MODEL, response.usage)
        for block in response.content:
            if block.type == "tool_use" and block.name == "record_places":
                findings = _parse(dict(block.input), index)
                findings.cost = spent
                return findings
        messages.append({"role": "assistant", "content": response.content})
        if response.stop_reason == "pause_turn" and spent < cap:
            continue
        # Finished talking without recording, or out of budget: make it record now.
        messages.append({"role": "user", "content": "Record your findings now with record_places."})
        force = True
    logger.warning("research run ended without record_places after %d turns", MAX_TURNS)
    return Findings(summary="Research didn't finish.", cost=spent)


def dossier(
    place_name: str,
    context: str,
    *,
    max_searches: int = 10,
    client: anthropic.Anthropic | None = None,
) -> tuple[str, float]:
    """A markdown research file for a place. Returns (markdown, cost)."""
    client = client or anthropic.Anthropic()
    messages: list[MessageParam] = [
        {
            "role": "user",
            "content": (f"Today: {date.today().isoformat()}\n\nPlace: {place_name}\n\n{context}"),
        }
    ]
    system = DOSSIER_PROMPT_PATH.read_text(encoding="utf-8")
    spent = 0.0
    text_parts: list[str] = []
    for _turn in range(MAX_TURNS):
        response = client.messages.create(
            model=llm.MAIN_MODEL,
            max_tokens=16000,
            system=system,
            tools=[_web_search(max_searches)],
            messages=messages,
        )
        spent += llm.record("dossier", llm.MAIN_MODEL, response.usage)
        text_parts = [b.text for b in response.content if b.type == "text"]
        if response.stop_reason != "pause_turn":
            break
        messages.append({"role": "assistant", "content": response.content})
    return "".join(text_parts).strip(), spent


# -- applying confirmed proposals (pure) --------------------------------------


def apply_new_places(
    files: dict[str, Any], proposals: list[dict[str, Any]], today: date
) -> dict[str, str]:
    """Add confirmed proposals to the place files, in place.

    Returns ref -> new id. Parents given by `parent_ref` are created first
    (in list order, repeatedly, until nothing more resolves), so a new
    town and the new restaurant inside it can be confirmed together.
    """
    created: dict[str, str] = {}
    remaining = list(proposals)
    progress = True
    while remaining and progress:
        progress = False
        index = PlaceIndex([n for nodes in files.values() for n in nodes])
        for p in list(remaining):
            parent_id = p.get("parent_id") if p.get("parent_id") in index.by_id else None
            if parent_id is None and p.get("parent_ref"):
                parent_id = created.get(p["parent_ref"])
                if parent_id is None:
                    continue
            node_id = new_node_id(index, str(p["name"]), parent_id)
            node: dict[str, Any] = {"id": node_id, "name": p["name"], "kind": p["kind"]}
            if parent_id:
                node["parent"] = parent_id
            for key in ("aliases", "lat", "lon", "confidence", "description", "facts"):
                if p.get(key) not in (None, [], {}, ""):
                    node[key] = p[key]
            if p.get("sources"):
                node["sources"] = [{"url": u, "fetched": today} for u in p["sources"]]
            # A new region gets its own file; anything else joins its region's.
            is_area = p["kind"] in ("region", "park")
            stem = node_id if is_area else file_for(index, parent_id)
            files.setdefault(stem, []).append(node)
            created[str(p.get("ref") or node_id)] = node_id
            remaining.remove(p)
            progress = True
            index = PlaceIndex([n for nodes in files.values() for n in nodes])
    return created


def _norm(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", text.lower()).strip()


def link_mentions(frontmatter: dict[str, Any], mention_to_id: dict[str, str]) -> int:
    """Point journal items whose place_mention matches at the resolved id.

    Mutates `frontmatter`; returns how many items were linked.
    """
    wanted = {_norm(k): v for k, v in mention_to_id.items()}
    linked = 0
    for field_name in ("activities", "meals"):
        for item in frontmatter.get(field_name) or []:
            if not isinstance(item, dict) or item.get("place"):
                continue
            mention = item.get("place_mention")
            if mention and _norm(str(mention)) in wanted:
                item["place"] = wanted[_norm(str(mention))]
                item.pop("place_mention", None)
                item.pop("place_kind", None)
                linked += 1
    return linked


@dataclass
class Mention:
    day: date
    text: str
    kind: str | None
    detail: str


def unresolved_mentions(day: date, frontmatter: dict[str, Any]) -> list[Mention]:
    out: list[Mention] = []
    seen: set[str] = set()
    for field_name in ("activities", "meals"):
        for item in frontmatter.get(field_name) or []:
            if not isinstance(item, dict) or item.get("place") or not item.get("place_mention"):
                continue
            key = _norm(str(item["place_mention"]))
            if key in seen:
                continue
            seen.add(key)
            detail = item.get("detail") or ", ".join(str(i) for i in item.get("items") or [])
            out.append(
                Mention(
                    day=day,
                    text=str(item["place_mention"]),
                    kind=item.get("place_kind"),
                    detail=str(detail),
                )
            )
    return out


# -- job prompts ---------------------------------------------------------------


def resolve_job(mentions: list[Mention], where: str) -> str:
    lines = [
        f'- {m.day.isoformat()}: "{m.text}" ({m.kind or "kind unknown"}) — {m.detail}'
        for m in mentions
    ]
    return (
        "JOB: resolve\n"
        f"The user was based in: {where}\n"
        "These place names came from the user's voice journal and aren't in Known places. "
        "For each: if it's a Known place under another name, put it in `matches`. "
        "Otherwise find what and where it is and propose it in `new_places` with the "
        "mention copied verbatim. Search when you need to; a warung with no web presence "
        "can still be proposed with confidence 'inferred' under the town the user was in. "
        "If you truly can't tell, list it under `unresolved`.\n\n" + "\n".join(lines)
    )


def arrive_job(region_path: str, activities: list[str], known_spots: list[str]) -> str:
    return (
        "JOB: arrive\n"
        f"The user just arrived in: {region_path}\n"
        f"They care about: {', '.join(activities) or 'surf, diving, hiking, food'}\n"
        f"Already known here: {', '.join(known_spots) or '(nothing)'}\n"
        "Find the places in and around this area worth knowing for those activities — "
        "for surf, every named break within day-trip reach with break direction, the "
        "tide and swell it works on, level, and hazards. Include the notable dive "
        "sites, hikes, viewpoints and one or two well-regarded food spots. Skip what's "
        "already known. Parent each under the right town or region; create the town "
        "first (with a ref) if it's missing. Where sources disagree, say so in facts."
    )


def trip_job(stay_label: str, days: list[tuple[date, str]]) -> str:
    lines = [f"- {d.isoformat()}: {summary}" for d, summary in days]
    return (
        "JOB: trip\n"
        f"Trip: {stay_label}\n"
        "The user's own journal for each day of the trip is below. Research the "
        "standard route for this kind of trip, then match it to what the journal "
        "describes, day by day. Propose each stop the user actually made as a place "
        "(or a `matches` entry if it's already known) with `day` set. Only stops the "
        "journal supports — mark ones identified only by matching the standard route "
        "with confidence 'inferred'.\n\n" + "\n".join(lines)
    )


def summarize_proposal(index: PlaceIndex, p: dict[str, Any], by_ref: dict[str, str]) -> str:
    """One confirm-card line: name, kind, full location path, confidence."""
    parent = p.get("parent_id")
    where = (
        index.path_name(parent)
        if parent in index.by_id
        else by_ref.get(p.get("parent_ref", ""), "?")
    )
    conf = p.get("confidence", "?")
    day = f" [{p['day']}]" if p.get("day") else ""
    return f"{p['name']} ({p['kind']}) — {where} · {conf}{day}"
