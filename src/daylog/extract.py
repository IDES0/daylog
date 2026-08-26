"""Transcript text -> structured journal facts, via a single Claude tool call."""

from __future__ import annotations

import logging
from datetime import date
from pathlib import Path
from typing import Any

import anthropic
from anthropic.types import MessageParam, ToolChoiceToolParam, ToolParam

logger = logging.getLogger(__name__)

MODEL = "claude-sonnet-5"
PROMPT_PATH = Path(__file__).parent / "prompts" / "extract.md"

RECORD_JOURNAL_ENTRY_TOOL: ToolParam = {
    "name": "record_journal_entry",
    "description": "Record structured facts extracted from a voice journal transcript.",
    "input_schema": {
        "type": "object",
        "properties": {
            "location": {
                "type": "string",
                "description": "Where the user was, e.g. 'Canggu, Bali'.",
            },
            "activities": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "type": {
                            "type": "string",
                            "description": "Short activity label, e.g. 'surf'.",
                        },
                        "hours": {"type": "number", "description": "Approximate hours spent."},
                        "detail": {"type": "string", "description": "One-line specifics."},
                    },
                    "required": ["type"],
                },
            },
            "goal_progress": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "goal_id": {
                            "type": "string",
                            "description": "Must be one of the ids in the provided goal list.",
                        },
                        "delta": {"type": "number"},
                        "detail": {"type": "string"},
                    },
                    "required": ["goal_id", "delta"],
                },
            },
            "goal_slips": {
                "type": "array",
                "description": (
                    "Only when the user explicitly asks to move a goal's deadline or "
                    "target window — not implied by missing a session."
                ),
                "items": {
                    "type": "object",
                    "properties": {
                        "goal_id": {
                            "type": "string",
                            "description": "Must be one of the ids in the provided goal list.",
                        },
                        "new_date": {
                            "type": "string",
                            "description": "ISO date (YYYY-MM-DD) the user wants to move to.",
                        },
                        "reason": {"type": "string"},
                    },
                    "required": ["goal_id", "new_date"],
                },
            },
            "itinerary_changes": {
                "type": "array",
                "description": (
                    "Only when the user talks about travel plans — a new place "
                    "they're considering, committing to, dropping, or a date "
                    "(visa expiry, firm commitment) being set or moved."
                ),
                "items": {
                    "type": "object",
                    "properties": {
                        "id": {
                            "type": "string",
                            "description": (
                                "Existing itinerary id being updated. Omit entirely "
                                "when this is a new place, not one already listed."
                            ),
                        },
                        "place": {
                            "type": "string",
                            "description": "Required when id is omitted (a new place).",
                        },
                        "type": {
                            "type": "string",
                            "enum": ["hard", "soft"],
                            "description": (
                                "Only for a new entry. 'hard' is a firm date that "
                                "can't move quietly — a visa expiry, a booked flight. "
                                "'soft' is a rough plan or candidate destination."
                            ),
                        },
                        "status": {
                            "type": "string",
                            "enum": ["candidate", "planned", "current", "done", "dropped"],
                        },
                        "new_date": {
                            "type": "string",
                            "description": "ISO date (YYYY-MM-DD) being set or moved to.",
                        },
                        "reason": {"type": "string"},
                        "notes": {"type": "string"},
                    },
                    "required": [],
                },
            },
            "corrections": {
                "type": "array",
                "description": (
                    "Only when the transcript explicitly corrects or retracts something "
                    "in 'Already logged today' below — e.g. 'actually I only surfed 1 "
                    "hour, not 2' or 'scratch that, I didn't skip the gym after all.' "
                    "Never infer a correction just because today's real activities "
                    "differ from something said earlier describing a different thing — "
                    "only an explicit correction/retraction counts. Only activities, "
                    "skipped, and open_questions can be corrected this way; goal and "
                    "itinerary corrections go through goal_slips/itinerary_changes."
                ),
                "items": {
                    "type": "object",
                    "properties": {
                        "field": {
                            "type": "string",
                            "enum": ["activities", "skipped", "open_questions"],
                        },
                        "index": {
                            "type": "integer",
                            "description": (
                                "0-based index into that field's numbered list in "
                                "'Already logged today' below."
                            ),
                        },
                        "reason": {
                            "type": "string",
                            "description": "Why, in the user's own words, if stated.",
                        },
                    },
                    "required": ["field", "index"],
                },
            },
            "skipped": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Things the user said they meant to do but skipped.",
            },
            "mood": {"type": "string"},
            "open_questions": {
                "type": "array",
                "items": {"type": "string"},
            },
            "summary": {
                "type": "string",
                "description": "Two or three plain sentences summarising the day.",
            },
        },
        "required": ["activities", "summary"],
    },
}


class ExtractError(RuntimeError):
    pass


def _format_goals(goals: list[dict[str, Any]]) -> str:
    if not goals:
        return "(no active goals)"
    lines = []
    for goal in goals:
        metric = f", metric: {goal['metric']}" if goal.get("metric") else ""
        lines.append(f'- id: {goal["id"]}, title: "{goal["title"]}", type: {goal["type"]}{metric}')
    return "\n".join(lines)


def _format_itinerary(itinerary: list[dict[str, Any]]) -> str:
    if not itinerary:
        return "(no itinerary entries yet)"
    lines = []
    for entry in itinerary:
        date_field = f", date: {entry['date']}" if entry.get("date") else ""
        lines.append(
            f'- id: {entry["id"]}, place: "{entry["place"]}", type: {entry["type"]}, '
            f"status: {entry.get('status', 'candidate')}{date_field}"
        )
    return "\n".join(lines)


def _describe_activity(item: dict[str, Any]) -> str:
    hours = f", {item['hours']:g}h" if item.get("hours") is not None else ""
    detail = f" — {item['detail']}" if item.get("detail") else ""
    return f"{item.get('type', '?')}{hours}{detail}"


def _format_existing_entry(frontmatter: dict[str, Any] | None) -> str:
    """Numbered listing of today's activities/skipped/open_questions, for corrections.

    Only these three fields are listed — they're the only ones `corrections`
    can target (see the tool schema). Indices must match what's actually in
    the entry, so the model can reference "index 1" and mean the same item
    the caller will later remove.
    """
    if not frontmatter:
        return "(nothing logged yet today)"

    blocks = []
    activities = frontmatter.get("activities")
    if activities:
        numbered = "\n".join(f"  {i}: {_describe_activity(a)}" for i, a in enumerate(activities))
        blocks.append(f"activities:\n{numbered}")
    for field in ("skipped", "open_questions"):
        items = frontmatter.get(field)
        if items:
            numbered = "\n".join(f"  {i}: {item}" for i, item in enumerate(items))
            blocks.append(f"{field}:\n{numbered}")
    return "\n".join(blocks) if blocks else "(nothing logged yet today)"


def extract(
    transcript: str,
    goals: list[dict[str, Any]],
    itinerary: list[dict[str, Any]],
    today: date,
    *,
    existing_frontmatter: dict[str, Any] | None = None,
    client: anthropic.Anthropic | None = None,
) -> dict[str, Any]:
    """Extract structured journal facts from a raw transcript.

    `goals` is the current active goal list (id/title/type/metric) so the
    model can resolve mentions to a real goal_id instead of inventing one.
    `itinerary` is the current travel plan (id/place/type/status/date) for
    the same reason, used to resolve itinerary_changes. `today` grounds
    relative date phrases ("push it a month") in goal_slips/itinerary dates.
    `existing_frontmatter` is the day's journal entry so far (if any,
    keyed by entry date, not necessarily today when backdating), so the
    model can resolve `corrections` against it by index.

    Returns a dict matching the journal frontmatter schema, plus a
    `summary` key the caller should pull out before writing to the vault.
    """
    client = client or anthropic.Anthropic()
    system_prompt = PROMPT_PATH.read_text(encoding="utf-8")

    tool_choice: ToolChoiceToolParam = {"type": "tool", "name": "record_journal_entry"}
    user_content = (
        f"Today's date: {today.isoformat()}\n\n"
        f"Already logged today (for corrections only):\n"
        f"{_format_existing_entry(existing_frontmatter)}\n\n"
        f"Current goals:\n{_format_goals(goals)}\n\n"
        f"Current itinerary:\n{_format_itinerary(itinerary)}\n\n"
        f"Transcript:\n{transcript}"
    )
    messages: list[MessageParam] = [{"role": "user", "content": user_content}]

    response = client.messages.create(
        model=MODEL,
        max_tokens=2048,
        system=system_prompt,
        tools=[RECORD_JOURNAL_ENTRY_TOOL],
        tool_choice=tool_choice,
        messages=messages,
    )

    logger.info(
        "extract usage: input=%d output=%d",
        response.usage.input_tokens,
        response.usage.output_tokens,
    )

    for block in response.content:
        if block.type == "tool_use":
            return dict(block.input)

    raise ExtractError(f"no tool_use block in response (stop_reason={response.stop_reason})")
