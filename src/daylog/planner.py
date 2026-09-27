"""The planner: 2-3 concrete, soft route options for the coming weeks.

One Claude call (with web search) over everything that bears on "where
next, when": where the user is, their goals and progress, hard deadlines,
the wishlist with its research files, recent days, and the date table.
It hands back options through a `record_plan` tool; each option is a
sequence of legs (place, dates, why, how to get there, rough cost).

Nothing is applied until the user picks an option — then each leg
becomes a `planned` itinerary entry with its date window. Plans are saved
as plans/YYYY-MM-DD.md so the brief and chat can see the current one.
Pure apart from the API call; no vault access here.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Any

import anthropic
from anthropic.types import ToolParam

from daylog import itinerary, llm

logger = logging.getLogger(__name__)

PROMPT_PATH = Path(__file__).parent / "prompts" / "planner.md"
MAX_TURNS = 8

RECORD_PLAN_TOOL: ToolParam = {
    "name": "record_plan",
    "description": "Hand back the plan options. Call exactly once, at the end.",
    "input_schema": {
        "type": "object",
        "properties": {
            "situation": {
                "type": "string",
                "description": "Two sentences: what constrains the next few weeks.",
            },
            "options": {
                "type": "array",
                "minItems": 1,
                "maxItems": 3,
                "items": {
                    "type": "object",
                    "properties": {
                        "label": {"type": "string", "description": "A few words."},
                        "summary": {"type": "string"},
                        "legs": {
                            "type": "array",
                            "items": {
                                "type": "object",
                                "properties": {
                                    "place": {"type": "string"},
                                    "place_id": {"type": "string"},
                                    "itinerary_id": {
                                        "type": "string",
                                        "description": "Existing wishlist id, if this is one.",
                                    },
                                    "start": {"type": "string", "description": "ISO date"},
                                    "end": {"type": "string", "description": "ISO date"},
                                    "why": {"type": "string"},
                                    "goal_ids": {"type": "array", "items": {"type": "string"}},
                                    "travel": {
                                        "type": "string",
                                        "description": "How to get there from the previous leg.",
                                    },
                                    "cost_usd": {"type": "number"},
                                },
                                "required": ["place", "start", "end", "why"],
                            },
                        },
                        "tradeoffs": {"type": "string"},
                    },
                    "required": ["label", "summary", "legs"],
                },
            },
            "recommended": {"type": "integer", "description": "0-based index of your pick."},
            "questions": {
                "type": "array",
                "items": {"type": "string"},
                "description": "What you'd want to know from the user to sharpen this.",
            },
        },
        "required": ["situation", "options", "recommended"],
    },
}


@dataclass
class Plan:
    situation: str
    options: list[dict[str, Any]]
    recommended: int
    questions: list[str] = field(default_factory=list)
    cost: float = 0.0


def run(context: str, *, max_searches: int = 8, client: anthropic.Anthropic | None = None) -> Plan:
    client = client or anthropic.Anthropic()
    system = PROMPT_PATH.read_text(encoding="utf-8")
    messages: list[Any] = [{"role": "user", "content": context}]
    tools: list[Any] = [
        {"type": "web_search_20260209", "name": "web_search", "max_uses": max_searches},
        RECORD_PLAN_TOOL,
    ]
    spent = 0.0
    force = False
    for _ in range(MAX_TURNS):
        kwargs: dict[str, Any] = {}
        if force:
            kwargs["tool_choice"] = {"type": "tool", "name": "record_plan"}
        response = llm.create(
            client,
            model=llm.MAIN_MODEL,
            max_tokens=16000,
            system=system,
            tools=tools,
            messages=messages,
            output_config={"effort": "high"},
            **kwargs,
        )
        spent += llm.record("plan", llm.MAIN_MODEL, response.usage)
        for block in response.content:
            if block.type == "tool_use" and block.name == "record_plan":
                raw = dict(block.input)
                options = [
                    o for o in raw.get("options") or [] if isinstance(o, dict) and o.get("legs")
                ]
                rec = raw.get("recommended", 0)
                return Plan(
                    situation=str(raw.get("situation", "")),
                    options=options,
                    recommended=rec if isinstance(rec, int) and 0 <= rec < len(options) else 0,
                    questions=[str(q) for q in raw.get("questions") or []],
                    cost=spent,
                )
        messages.append({"role": "assistant", "content": response.content})
        if response.stop_reason == "pause_turn" and spent < llm.research_run_budget():
            continue
        messages.append({"role": "user", "content": "Record the plan now with record_plan."})
        force = True
    return Plan(situation="The planner didn't finish.", options=[], recommended=0, cost=spent)


def render(plan: Plan, made_on: date) -> str:
    """Markdown for plans/YYYY-MM-DD.md and for the Telegram message."""
    lines = [f"# Plan — {made_on.isoformat()}", "", plan.situation, ""]
    for i, option in enumerate(plan.options):
        star = " (recommended)" if i == plan.recommended else ""
        lines += [
            f"## {chr(65 + i)}. {option.get('label')}{star}",
            "",
            str(option.get("summary", "")),
        ]
        for leg in option.get("legs") or []:
            cost = (
                f" · ~${leg['cost_usd']:.0f}"
                if isinstance(leg.get("cost_usd"), int | float)
                else ""
            )
            travel = f" — {leg['travel']}" if leg.get("travel") else ""
            lines.append(
                f"- {leg.get('start')} → {leg.get('end')}: {leg.get('place')}{cost}{travel}"
            )
            if leg.get("why"):
                lines.append(f"  {leg['why']}")
        if option.get("tradeoffs"):
            lines += ["", f"Trade-offs: {option['tradeoffs']}"]
        lines.append("")
    if plan.questions:
        lines += ["## Questions", *[f"- {q}" for q in plan.questions], ""]
    return "\n".join(lines).rstrip() + "\n"


def apply_option(itinerary_data: list[Any], option: dict[str, Any], on: date) -> list[str]:
    """Turn a chosen option's legs into `planned` itinerary entries with date windows.

    Matches an existing entry by itinerary_id, then place_id; otherwise adds
    one. Soft windows only — a hard deadline is never set from a plan.
    """
    applied = []
    for leg in option.get("legs") or []:
        try:
            start = date.fromisoformat(str(leg["start"]))
            end = date.fromisoformat(str(leg["end"]))
        except (KeyError, ValueError):
            continue
        entry = itinerary.find_entry(itinerary_data, str(leg.get("itinerary_id", "")))
        if entry is None and leg.get("place_id"):
            entry = next((e for e in itinerary_data if e.get("place_id") == leg["place_id"]), None)
        if entry is None:
            change: dict[str, Any] = {"place": leg.get("place"), "status": "planned"}
            if leg.get("place_id"):
                change["place_id"] = leg["place_id"]
            done, _ = itinerary.apply_itinerary_changes(itinerary_data, [change], on=on)
            entry = itinerary.find_entry(itinerary_data, done[0].id)
        if entry is None or entry.get("type") == "hard":
            continue
        entry["status"] = "planned"
        entry["target_window"] = [start, end]
        for goal_id in leg.get("goal_ids") or []:
            why = entry.get("why")
            if why is None:
                why = []
                entry["why"] = why
            if goal_id not in why:
                why.append(goal_id)
        applied.append(f"{entry.get('place')}: {start.isoformat()} → {end.isoformat()}")
    return applied
