"""The chat assistant: Claude with read access to the vault, web search, and
proposal tools.

Typed text that isn't a journal entry lands here. The assistant can read
anything in the vault through tools (journal, trail, places, rankings,
research files) and search the web; it can *change* things only by
proposing — each proposal becomes a confirm card the user taps, the same
rule as research. The one exception is a note on a known place (the
user's own words about it), which is written straight away.

Pure apart from the API call and the Vault passed in: the Telegram side
(chat_flow.py) owns history, cards, and sending.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

import anthropic
from anthropic.types import MessageParam, ToolParam

from daylog import brief, goals, llm, rankings, trail
from daylog.places import PlaceIndex
from daylog.vault import Vault

logger = logging.getLogger(__name__)

PROMPT_PATH = Path(__file__).parent / "prompts" / "chat.md"
MAX_TURNS = 10
# Keep this many recent messages as conversation history.
HISTORY_LIMIT = 30


def _tool(
    name: str, description: str, properties: dict[str, Any], required: list[str]
) -> ToolParam:
    return {
        "name": name,
        "description": description,
        "input_schema": {"type": "object", "properties": properties, "required": required},
    }


TOOLS: list[ToolParam] = [
    _tool(
        "get_journal",
        "Journal summaries and structured facts (activities, meals, felt, goal progress) "
        "for a date range, inclusive. Max 31 days per call.",
        {"start": {"type": "string"}, "end": {"type": "string"}},
        ["start", "end"],
    ),
    _tool(
        "get_trail",
        "Where the user was based and which places they went, day by day, for a date range.",
        {"start": {"type": "string"}, "end": {"type": "string"}},
        ["start", "end"],
    ),
    _tool(
        "find_places",
        "Search the places tree by name, kind and/or region id. Returns ids, kinds and paths.",
        {
            "query": {"type": "string"},
            "kind": {"type": "string"},
            "region_id": {"type": "string"},
        },
        [],
    ),
    _tool(
        "get_place",
        "Everything known about one place: description, facts, notes, visits, rank.",
        {"place_id": {"type": "string"}},
        ["place_id"],
    ),
    _tool(
        "get_rankings",
        "The user's ranked list for a category: food, surf, dive, stay, outdoors, nightlife.",
        {"category": {"type": "string"}},
        ["category"],
    ),
    _tool(
        "get_research",
        "The research file for a place id, if one exists (seasons, events, getting there).",
        {"place_id": {"type": "string"}},
        ["place_id"],
    ),
    _tool(
        "add_place_note",
        "Save something the user said about a known place to its notes. Written immediately.",
        {"place_id": {"type": "string"}, "text": {"type": "string"}},
        ["place_id", "text"],
    ),
    _tool(
        "propose_itinerary_change",
        "Propose adding/updating a wishlist destination or trip date. The user confirms "
        "with a button before anything changes. One call per change.",
        {
            "id": {"type": "string", "description": "Existing itinerary id, if updating."},
            "place": {"type": "string"},
            "place_id": {"type": "string"},
            "status": {"type": "string", "enum": ["candidate", "planned", "done", "dropped"]},
            "new_date": {"type": "string", "description": "ISO date (target or deadline)."},
            "why": {"type": "array", "items": {"type": "string"}},
            "notes": {"type": "string"},
            "summary": {"type": "string", "description": "One line shown on the confirm card."},
        },
        ["summary"],
    ),
    _tool(
        "start_research",
        "Start a background research job: 'explore' (spots around the user now) or "
        "'dossier' (a research file on place_or_name). Uses the API budget; results arrive "
        "as separate messages later.",
        {
            "job": {"type": "string", "enum": ["explore", "dossier"]},
            "place_or_name": {"type": "string"},
        },
        ["job"],
    ),
]


@dataclass
class ChatResult:
    text: str
    proposals: list[dict[str, Any]] = field(default_factory=list)
    research_jobs: list[dict[str, Any]] = field(default_factory=list)
    new_history: list[MessageParam] = field(default_factory=list)
    cost: float = 0.0


def _parse_day(value: Any, default: date) -> date:
    try:
        return date.fromisoformat(str(value))
    except ValueError:
        return default


class Tools:
    """Client-side tool implementations over one vault snapshot."""

    def __init__(self, v: Vault, today: date) -> None:
        self.v = v
        self.today = today
        self.index = PlaceIndex(v.read_places())
        self.proposals: list[dict[str, Any]] = []
        self.research_jobs: list[dict[str, Any]] = []

    def run(self, name: str, args: dict[str, Any]) -> str:
        handler = getattr(self, f"t_{name}", None)
        if handler is None:
            return f"unknown tool {name}"
        try:
            return str(handler(**args))
        except TypeError as exc:
            return f"bad arguments: {exc}"

    def _range(self, start: Any, end: Any) -> tuple[date, date]:
        e = _parse_day(end, self.today)
        s = _parse_day(start, e - timedelta(days=6))
        if (e - s).days > 31:
            s = e - timedelta(days=31)
        return s, e

    def t_get_journal(self, start: str, end: str) -> str:
        s, e = self._range(start, end)
        entries = self.v.read_journal_range(s, e)
        if not entries:
            return "No entries in that range."
        blocks = []
        for d, entry in sorted(entries.items()):
            fm = {k: v for k, v in entry.frontmatter.items() if k != "date"}
            blocks.append(f"## {d.isoformat()}\n{entry.summary}\n{json.dumps(fm, default=str)}")
        return "\n\n".join(blocks)

    def t_get_trail(self, start: str, end: str) -> str:
        s, e = self._range(start, end)
        entries = self.v.read_journal_range(s, e)
        days = trail.build(
            self.v.read_location(),
            {d: x.frontmatter for d, x in entries.items()},
            {},
            s,
            e,
        )
        # Kinds matter here: without them 'The Field' (a restaurant) reads like a spot.
        return trail.format_trail(days, self.index, with_kinds=True)

    def t_find_places(self, query: str = "", kind: str = "", region_id: str = "") -> str:
        nodes = list(self.index.by_id.values())
        if region_id and region_id in self.index.by_id:
            nodes = self.index.descendants(region_id)
        if kind:
            nodes = [n for n in nodes if n.get("kind") == kind]
        if query:
            q = query.lower()
            nodes = [
                n
                for n in nodes
                if q in str(n.get("name", "")).lower()
                or any(q in str(a).lower() for a in n.get("aliases") or [])
            ]
        lines = [
            f"{n['id']} ({n.get('kind')}): {self.index.path_name(n['id'])}" for n in nodes[:60]
        ]
        return "\n".join(lines) or "No matching places."

    def t_get_place(self, place_id: str) -> str:
        node = self.index.get(place_id)
        if node is None:
            return f"No place with id {place_id}."
        journal = self.v.read_journal_range(date(2000, 1, 1), self.today)
        visited = trail.visits({d: e.frontmatter for d, e in journal.items()}, self.index)
        category = rankings.category_for(node.get("kind"))
        rank = (
            rankings.describe(self.v.read_yaml("rankings.yaml", {}), category, place_id)
            if category
            else None
        )
        data = {k: v for k, v in node.items()}
        data["path"] = self.index.path_name(place_id)
        data["visited"] = [d.isoformat() for d in visited.get(place_id, [])]
        data["rank"] = rank
        return json.dumps(data, default=str)

    def t_get_rankings(self, category: str) -> str:
        current = self.v.read_yaml("rankings.yaml", {})
        order = rankings.ranked(current, category)
        if not order:
            return f"Nothing ranked in {category}."
        return "\n".join(
            f"{i}. {self.index.path_name(pid)} — {rankings.score(current, category, pid)}"
            for i, (pid, _tier) in enumerate(order, start=1)
        )

    def t_get_research(self, place_id: str) -> str:
        return self.v.read_text(f"research/{place_id}.md") or "No research file for that place yet."

    def t_add_place_note(self, place_id: str, text: str) -> str:
        files = self.v.read_place_files()
        for stem, nodes in files.items():
            for node in nodes:
                if node.get("id") == place_id:
                    notes = node.get("my_notes")
                    if notes is None:
                        notes = []
                        node["my_notes"] = notes
                    notes.append({"date": self.today, "text": text})
                    self.v.write_place_files(files, {stem}, f"places: note on {place_id}")
                    return "Saved."
        return f"No place with id {place_id}."

    def t_propose_itinerary_change(self, **change: Any) -> str:
        self.proposals.append(change)
        return "Proposed — the user will see a confirm button after your reply."

    def t_start_research(self, job: str, place_or_name: str = "") -> str:
        self.research_jobs.append({"job": job, "place": place_or_name})
        return "Started — results arrive as separate messages."


def context_block(v: Vault, today: date, now: datetime) -> str:
    """Volatile per-message context: where the user is, goals, wishlist, recent days."""
    index = PlaceIndex(v.read_places())
    location = brief.current_location(v.read_location())
    node = index.current_place(location)
    where = index.path_name(node["id"]) if node else (location or {}).get("place", "unknown")
    goal_lines = []
    for g in v.read_goals():
        if g.get("status", "active") != "active":
            continue
        target = goals.current_target_date(g)
        progress = f"{g.get('progress', 0):g} {g['metric']}" if g.get("metric") else ""
        goal_lines.append(
            f"- {g['id']}: {g.get('title')} ({g.get('type', 'soft')}) {progress}"
            + (f" target {target}" if target else "")
        )
    wish_lines = [
        f"- {e['id']}: {e.get('place')} [{e.get('status')}] research={e.get('research', 'none')}"
        for e in v.read_itinerary()
        if e.get("status") in ("candidate", "planned")
    ]
    recent = brief.recent_journal_summaries(v, today + timedelta(days=1), days=4)
    return (
        f"Now: {now.strftime('%Y-%m-%d %H:%M %A')}\n"
        f"User is based in: {where}\n\n"
        f"Active goals:\n{chr(10).join(goal_lines) or '(none)'}\n\n"
        f"Wishlist:\n{chr(10).join(wish_lines) or '(empty)'}\n\n"
        f"Last few days:\n{recent}"
    )


def trim_history(history: list[MessageParam], limit: int = HISTORY_LIMIT) -> list[MessageParam]:
    """Keep the last `limit` messages, starting on a plain user text turn so no
    tool_result is left without its tool_use."""
    trimmed = history[-limit:]
    while trimmed:
        first = trimmed[0]
        if first["role"] == "user" and isinstance(first["content"], str):
            break
        trimmed = trimmed[1:]
    return trimmed


def respond(
    v: Vault,
    history: list[MessageParam],
    user_text: str,
    now: datetime,
    *,
    allow_web: bool = True,
    client: anthropic.Anthropic | None = None,
) -> ChatResult:
    client = client or anthropic.Anthropic()
    today = now.date()
    tools_impl = Tools(v, today)
    system = PROMPT_PATH.read_text(encoding="utf-8")
    # Plain dicts: this turn's tool traffic is built from model_dump()s and
    # tool_result dicts, which the SDK accepts but its TypedDicts don't describe.
    turn: list[Any] = [
        {
            "role": "user",
            "content": f"<context>\n{context_block(v, today, now)}\n</context>\n\n{user_text}",
        }
    ]
    tools: list[Any] = list(TOOLS)
    if allow_web:
        tools.append({"type": "web_search_20260209", "name": "web_search", "max_uses": 5})
    spent = 0.0
    text = ""
    for _ in range(MAX_TURNS):
        response = llm.create(
            client,
            model=llm.MAIN_MODEL,
            max_tokens=8000,
            system=[{"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}],
            tools=tools,
            messages=[*history, *turn],
            output_config={"effort": "medium"},
        )
        spent += llm.record("chat", llm.MAIN_MODEL, response.usage)
        content = [block.model_dump(exclude_none=True) for block in response.content]
        turn.append({"role": "assistant", "content": content})
        text = "".join(b.text for b in response.content if b.type == "text").strip()
        if response.stop_reason == "pause_turn":
            continue
        if response.stop_reason != "tool_use":
            break
        results = []
        for block in response.content:
            if block.type == "tool_use":
                output = tools_impl.run(block.name, dict(block.input))
                results.append({"type": "tool_result", "tool_use_id": block.id, "content": output})
        turn.append({"role": "user", "content": results})
    # History keeps only the user's words and the final answer — tool traffic is
    # re-derivable and would crowd out older conversation.
    new_history: list[MessageParam] = [
        *history,
        {"role": "user", "content": user_text},
        {"role": "assistant", "content": text or "(no reply)"},
    ]
    return ChatResult(
        text=text or "I couldn't come up with an answer — try rephrasing?",
        proposals=tools_impl.proposals,
        research_jobs=tools_impl.research_jobs,
        new_history=trim_history(new_history),
        cost=spent,
    )
