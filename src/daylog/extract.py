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
            "location_change": {
                "type": "object",
                "description": (
                    "Only when the transcript explicitly states the user has moved to "
                    "or arrived at a new base — not just mentioning a place in "
                    "passing, describing where today's activity happened, or a "
                    "place they're merely considering (that goes through "
                    "itinerary_changes instead). This updates where the bot thinks "
                    "the user currently is, for the morning brief's forecasts and "
                    "place matching — separate from `location` above, which is only "
                    "descriptive text for today's entry. Omit if the current "
                    "location given below is already correct."
                ),
                "properties": {
                    "place": {
                        "type": "string",
                        "description": "Short current place name, e.g. 'Ubud, Bali, ID'.",
                    },
                    "lat": {
                        "type": "number",
                        "description": (
                            "Approximate latitude — only if it's a real, "
                            "identifiable place you're confident about."
                        ),
                    },
                    "lon": {
                        "type": "number",
                        "description": "Approximate longitude, only if confident.",
                    },
                },
                "required": ["place"],
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
            "other_day_notes": {
                "type": "array",
                "description": (
                    "Only for an explicit aside about a specific OTHER day, mentioned in "
                    "passing while the transcript is mainly about today — e.g. 'oh yeah, "
                    "yesterday I also went surfing, forgot to mention it.' This is for a "
                    "genuinely forgotten fact about a different day; it is NOT for the day "
                    "this whole message is about (a message that's entirely about a past "
                    "day is backdated before it's sent, not marked here) — if the whole "
                    "transcript is one continuous account of a single day, everything "
                    "belongs in the top-level fields, not here. Resolve relative phrases "
                    "('yesterday', 'Monday') into a real ISO date using today's actual date."
                ),
                "items": {
                    "type": "object",
                    "properties": {
                        "date": {
                            "type": "string",
                            "description": "ISO date (YYYY-MM-DD) the aside is actually about.",
                        },
                        "activities": {
                            "type": "array",
                            "items": {
                                "type": "object",
                                "properties": {
                                    "type": {"type": "string"},
                                    "hours": {"type": "number"},
                                    "detail": {"type": "string"},
                                },
                                "required": ["type"],
                            },
                        },
                        "skipped": {"type": "array", "items": {"type": "string"}},
                        "mood": {"type": "string"},
                        "summary": {
                            "type": "string",
                            "description": "One short sentence describing just this aside.",
                        },
                    },
                    "required": ["date", "summary"],
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


# Fields the schema declares as arrays. Observed failure mode: the model
# returns a technically-valid tool_use.input whose "activities" value is
# itself a JSON *string* encoding the entire intended payload (summary,
# mood, goal_progress and all) rather than following the schema — nothing
# in the SDK catches this, since `block.input` is already a parsed dict at
# that point, just one whose value for this key is a string instead of a
# list. Silently accepting it wrote that string straight into the vault as
# the literal `activities` frontmatter value, corrupting the entry and
# permanently losing whatever was buried in it (see journal 2026-09-16).
# Reject outright instead of guessing how to unpack it — the caller
# already turns an ExtractError into "try again," which is far better than
# writing bad data no one notices until something downstream crashes.
_ARRAY_FIELDS = (
    "activities",
    "goal_progress",
    "goal_slips",
    "itinerary_changes",
    "corrections",
    "other_day_notes",
    "skipped",
    "open_questions",
)


def _validate_shape(facts: dict[str, Any]) -> None:
    for field in _ARRAY_FIELDS:
        value = facts.get(field)
        if value is not None and not isinstance(value, list):
            raise ExtractError(
                f"malformed tool call: {field!r} was {type(value).__name__}, not a list"
            )
    summary = facts.get("summary")
    if not isinstance(summary, str) or not summary.strip():
        raise ExtractError("malformed tool call: summary was missing or empty")


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


def _format_known_spots(places_data: list[dict[str, Any]]) -> str:
    """Named surf/wind spots across every curated place, for grounding activity mentions.

    Whisper transcribes an unusual local place name poorly often enough
    that a garbled result (e.g. "Gerupuk" -> "group hook") gives the model
    nothing to recognize — with no real name to anchor to, it can default
    to a well-known place from general knowledge instead of the user's own
    actual, obscure spot. Listing every curated name up front (regardless
    of current location — a day trip elsewhere is exactly the case a
    location filter would get wrong) gives it something concrete to match
    a garbled mention against before falling back to a guess.
    """
    lines = []
    for place in places_data:
        for key in ("surf_spots", "wind_spots"):
            for spot in place.get(key, []):
                name = spot.get("name")
                if not name:
                    continue
                break_type = f", {spot['break_type']}" if spot.get("break_type") else ""
                lines.append(f"- {name} ({place.get('name', '?')}{break_type})")
    return "\n".join(lines) if lines else "(none curated yet)"


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
    current_location: str | None = None,
    places: list[dict[str, Any]] | None = None,
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
    model can resolve `corrections` against it by index. `current_location`
    is what location.yaml says the user's base is right now, so the model
    can tell an actual move apart from a place already correctly recorded.
    `places` is places.yaml's curated surf/wind spot names, so a garbled
    transcription of an obscure real spot has something concrete to match
    against instead of defaulting to a famous but wrong one.

    Returns a dict matching the journal frontmatter schema, plus a
    `summary` key the caller should pull out before writing to the vault.
    """
    client = client or anthropic.Anthropic()
    system_prompt = PROMPT_PATH.read_text(encoding="utf-8")

    tool_choice: ToolChoiceToolParam = {"type": "tool", "name": "record_journal_entry"}
    user_content = (
        f"Today's date: {today.isoformat()}\n\n"
        f"Current location (per location.yaml): {current_location or '(not set)'}\n\n"
        f"Already logged today (for corrections only):\n"
        f"{_format_existing_entry(existing_frontmatter)}\n\n"
        f"Current goals:\n{_format_goals(goals)}\n\n"
        f"Current itinerary:\n{_format_itinerary(itinerary)}\n\n"
        f"Known surf/wind spots (from places.yaml):\n{_format_known_spots(places or [])}\n\n"
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
            facts = dict(block.input)
            _validate_shape(facts)
            return facts

    raise ExtractError(f"no tool_use block in response (stop_reason={response.stop_reason})")
