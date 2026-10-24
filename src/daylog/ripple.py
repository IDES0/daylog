"""What one decision does to everything else: the ripple.

When a voice note states a decision that changes where the user will be
or what they can do for weeks (a job, a flight home, an injury), one
Claude call (with web search) traces it through the itinerary, goals and
current plan. It hands back, through a `record_ripple` tool: what the
decision fixes, what it disrupts item by item, the edits that bring the
itinerary and goals in line, ways to keep pursuing the affected goals
inside the new constraints, and what it opens up.

The edits are proposals in the chat's shape (edit: itinerary | goal), so
applying one goes through the same code as a chat confirm card. Nothing is
applied until the user taps. Pure apart from the API call; no vault access.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Any

import anthropic
from anthropic.types import ToolParam

from daylog import llm

logger = logging.getLogger(__name__)

PROMPT_PATH = Path(__file__).parent / "prompts" / "ripple.md"
MAX_TURNS = 6
MAX_SEARCHES = 5
EFFECTS = ("blocked", "moved", "at_risk", "helped")

RECORD_RIPPLE_TOOL: ToolParam = {
    "name": "record_ripple",
    "description": "Hand back the decision's ripple. Call exactly once, at the end.",
    "input_schema": {
        "type": "object",
        "properties": {
            "decision": {"type": "string", "description": "The decision, one line."},
            "fixes": {
                "type": "array",
                "items": {"type": "string"},
                "description": "The new constraints: where, which days and hours, money.",
            },
            "impacts": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "target": {
                            "type": "string",
                            "description": "Itinerary or goal id, or 'plan'.",
                        },
                        "label": {"type": "string", "description": "Its name, a few words."},
                        "effect": {"type": "string", "enum": list(EFFECTS)},
                        "detail": {"type": "string", "description": "One line, with dates."},
                    },
                    "required": ["target", "label", "effect", "detail"],
                },
            },
            "changes": {
                "type": "array",
                "description": "Edits to apply, each confirmed by the user.",
                "items": {
                    "type": "object",
                    "properties": {
                        "edit": {"type": "string", "enum": ["itinerary", "goal"]},
                        "id": {"type": "string", "description": "Itinerary id (edit=itinerary)."},
                        "goal_id": {"type": "string", "description": "Goal id (edit=goal)."},
                        "status": {
                            "type": "string",
                            "description": (
                                "itinerary: candidate/planned/done/dropped; "
                                "goal: active/done/dropped."
                            ),
                        },
                        "new_date": {
                            "type": "string",
                            "description": "itinerary: ISO end date or deadline.",
                        },
                        "date": {"type": "string", "description": "goal: ISO target date."},
                        "notes": {
                            "type": "string",
                            "description": "Replaces the existing notes in full.",
                        },
                        "summary": {"type": "string", "description": "One line for the card."},
                    },
                    "required": ["edit", "summary"],
                },
            },
            "workarounds": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "goal": {"type": "string", "description": "Which pursuit, a few words."},
                        "idea": {"type": "string"},
                        "when": {"type": "string"},
                        "cost": {"type": "string"},
                    },
                    "required": ["goal", "idea"],
                },
            },
            "opens": {"type": "array", "items": {"type": "string"}},
            "questions": {"type": "array", "items": {"type": "string"}},
        },
        "required": ["decision", "impacts", "changes", "workarounds"],
    },
}


@dataclass
class Ripple:
    decision: str
    fixes: list[str] = field(default_factory=list)
    impacts: list[dict[str, str]] = field(default_factory=list)
    changes: list[dict[str, Any]] = field(default_factory=list)
    workarounds: list[dict[str, str]] = field(default_factory=list)
    opens: list[str] = field(default_factory=list)
    questions: list[str] = field(default_factory=list)
    cost: float = 0.0


def _strings(value: Any) -> list[str]:
    return [str(x) for x in value or [] if x]


def parse(raw: dict[str, Any], cost: float = 0.0) -> Ripple:
    return Ripple(
        decision=str(raw.get("decision", "")),
        fixes=_strings(raw.get("fixes")),
        impacts=[
            {k: str(i.get(k, "")) for k in ("target", "label", "effect", "detail")}
            for i in raw.get("impacts") or []
            if isinstance(i, dict) and i.get("effect") in EFFECTS
        ],
        changes=[c for c in raw.get("changes") or [] if isinstance(c, dict) and c.get("summary")],
        workarounds=[
            {k: str(w[k]) for k in ("goal", "idea", "when", "cost") if w.get(k)}
            for w in raw.get("workarounds") or []
            if isinstance(w, dict) and w.get("idea")
        ],
        opens=_strings(raw.get("opens")),
        questions=_strings(raw.get("questions"))[:3],
        cost=cost,
    )


def valid_changes(
    changes: list[dict[str, Any]], itinerary_ids: set[str], goal_ids: set[str]
) -> list[dict[str, Any]]:
    """Only edits to entries that exist: a ripple adjusts the plan, it doesn't add to it."""
    out = []
    for change in changes:
        if change.get("edit") == "itinerary" and change.get("id") in itinerary_ids:
            out.append({k: v for k, v in change.items() if k != "goal_id"})
        elif change.get("edit") == "goal" and change.get("goal_id") in goal_ids:
            out.append({k: v for k, v in change.items() if k != "id"})
    return out


def run(
    context: str, *, max_searches: int = MAX_SEARCHES, client: anthropic.Anthropic | None = None
) -> Ripple:
    client = client or anthropic.Anthropic()
    system = PROMPT_PATH.read_text(encoding="utf-8")
    messages: list[Any] = [{"role": "user", "content": context}]
    tools: list[Any] = [*llm.web_tools(max_searches), RECORD_RIPPLE_TOOL]
    spent = 0.0
    force = False
    for _ in range(MAX_TURNS):
        kwargs: dict[str, Any] = {}
        if force:
            kwargs["tool_choice"] = {"type": "tool", "name": "record_ripple"}
        response = llm.create(
            client,
            model=llm.MAIN_MODEL,
            max_tokens=12000,
            system=system,
            tools=tools,
            messages=messages,
            output_config={"effort": "high"},
            **kwargs,
        )
        spent += llm.record("ripple", llm.MAIN_MODEL, response.usage)
        for block in response.content:
            if block.type == "tool_use" and block.name == "record_ripple":
                return parse(dict(block.input), spent)
        messages.append({"role": "assistant", "content": response.content})
        if response.stop_reason == "pause_turn" and spent < llm.research_run_budget():
            continue
        messages.append({"role": "user", "content": "Record it now with record_ripple."})
        force = True
    return Ripple(decision="The analysis didn't finish.", cost=spent)


_EFFECT_LABELS = {
    "blocked": "Blocked",
    "moved": "Moved",
    "at_risk": "At risk",
    "helped": "Helped",
}


def render(ripple: Ripple, made_on: date, heading: str = "#") -> str:
    """Markdown for the decision file and the app."""
    lines = [f"{heading} Decision: {ripple.decision}", f"_{made_on.isoformat()}_", ""]
    if ripple.fixes:
        lines += ["**What it fixes**", *[f"- {f}" for f in ripple.fixes], ""]
    for effect in EFFECTS:
        hits = [i for i in ripple.impacts if i["effect"] == effect]
        if hits:
            lines.append(f"**{_EFFECT_LABELS[effect]}**")
            lines += [f"- {i['label']}: {i['detail']}" for i in hits]
            lines.append("")
    if ripple.workarounds:
        lines.append("**Ways to keep going**")
        for w in ripple.workarounds:
            extra = " · ".join(x for x in (w.get("when"), w.get("cost")) if x)
            lines.append(f"- {w.get('goal', '')}: {w['idea']}" + (f" ({extra})" if extra else ""))
        lines.append("")
    if ripple.opens:
        lines += ["**What it opens up**", *[f"- {o}" for o in ripple.opens], ""]
    if ripple.questions:
        lines += ["**Questions**", *[f"- {q}" for q in ripple.questions], ""]
    return "\n".join(lines).rstrip() + "\n"


def slug(text: str) -> str:
    words = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-").split("-")
    return "-".join(words[:6]) or "decision"
