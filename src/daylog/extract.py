"""Transcript text -> structured journal facts, via a single Claude tool call."""

from __future__ import annotations

import json
import logging
from datetime import date
from pathlib import Path
from typing import Any, cast

import anthropic
from anthropic.types import MessageParam, ToolChoiceToolParam, ToolParam

from daylog import llm
from daylog.places import KINDS, PlaceIndex, outline

logger = logging.getLogger(__name__)

MODEL = llm.MAIN_MODEL
PROMPT_PATH = Path(__file__).parent / "prompts" / "extract.md"
RECONCILE_PROMPT_PATH = Path(__file__).parent / "prompts" / "reconcile.md"

# Shared by every journal list whose items happen somewhere (activities,
# meals): a resolved id when the place is already known, the name as said
# when it isn't — the research step resolves those later, with a confirm.
_PLACE_LINK_PROPERTIES: dict[str, Any] = {
    "place": {
        "type": "string",
        "description": (
            "Id from Known places where this happened. Only an id copied from "
            "that list — never invented."
        ),
    },
    "place_mention": {
        "type": "string",
        "description": (
            "When no Known places id fits: the place's proper name as the user "
            "said it (corrected for obvious transcription garbling). Only a real "
            "name — never a description like 'near Lakey' or 'a cafe'. Omit if "
            "no specific place was named."
        ),
    },
    "place_kind": {
        "type": "string",
        "enum": list(KINDS),
        "description": "With place_mention only: what kind of place it seems to be.",
    },
}

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
                    "place_id": {
                        "type": "string",
                        "description": "The Known places id for this base, when one matches.",
                    },
                    "mode": {
                        "type": "string",
                        "enum": ["stay", "trip", "transit"],
                        "description": (
                            "'trip' for a multi-day moving trip (a liveaboard, a trek "
                            "between camps), 'transit' for a travel day, else 'stay'."
                        ),
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
                        **_PLACE_LINK_PROPERTIES,
                    },
                    "required": ["type"],
                },
            },
            "meals": {
                "type": "array",
                "description": "Everything the user says they ate or drank, one item per meal.",
                "items": {
                    "type": "object",
                    "properties": {
                        "when": {
                            "type": "string",
                            "description": (
                                "'breakfast', 'lunch', 'dinner', 'snack', or a clock time "
                                "like '14:30' if stated."
                            ),
                        },
                        "items": {
                            "type": "array",
                            "items": {"type": "string"},
                            "description": "What was eaten/drunk, e.g. ['octopus', 'smoothie'].",
                        },
                        "cost": {
                            "type": "string",
                            "description": "As stated, with currency, e.g. '200k IDR'.",
                        },
                        "verdict": {
                            "type": "string",
                            "description": "The user's own take, if given ('incredible', 'mid').",
                        },
                        "kcal": {
                            "type": "integer",
                            "description": (
                                "Your rough estimate of the meal's calories, from the items "
                                "and any portion said (assume a normal serving otherwise)."
                            ),
                        },
                        "protein_g": {"type": "integer", "description": "Rough estimate, grams."},
                        "carbs_g": {"type": "integer", "description": "Rough estimate, grams."},
                        "fat_g": {"type": "integer", "description": "Rough estimate, grams."},
                        **_PLACE_LINK_PROPERTIES,
                    },
                    "required": ["items"],
                },
            },
            "felt": {
                "type": "array",
                "description": (
                    "How the user says they felt — physically or mentally — at a point in "
                    "the day. Only what's said, never inferred from activities."
                ),
                "items": {
                    "type": "object",
                    "properties": {
                        "when": {
                            "type": "string",
                            "description": "'morning', 'afternoon', 'evening', 'night' or a time.",
                        },
                        "energy": {
                            "type": "integer",
                            "minimum": 1,
                            "maximum": 5,
                            "description": "1 exhausted .. 5 great, only if clearly implied.",
                        },
                        "mood": {
                            "type": "integer",
                            "minimum": 1,
                            "maximum": 5,
                            "description": "1 low .. 5 great, only if clearly implied.",
                        },
                        "focus": {
                            "type": "integer",
                            "minimum": 1,
                            "maximum": 5,
                            "description": "1 scattered .. 5 locked in, only if clearly implied.",
                        },
                        "body": {
                            "type": "string",
                            "description": "Physical state: 'sore legs', 'upset stomach'.",
                        },
                        "mind": {
                            "type": "string",
                            "description": "Mental state: 'lazy', 'stoked', 'anxious'.",
                        },
                        "note": {"type": "string", "description": "Anything else, briefly."},
                    },
                    "required": [],
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
                    "Only when the user talks about travel plans — a destination "
                    "they want to go to, commit to, or drop, or a date (visa "
                    "expiry, firm commitment) being set or moved. Never a spot "
                    "near where they are now, and never where they already are."
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
                        "place_id": {
                            "type": "string",
                            "description": "Known places id for this destination, if listed.",
                        },
                        "why": {
                            "type": "array",
                            "items": {"type": "string"},
                            "description": "Goal ids from the goal list this trip serves.",
                        },
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
EXTRACT_ATTEMPTS = 2

_ARRAY_FIELDS = (
    "activities",
    "meals",
    "felt",
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
    places: list[Any] | None = None,
    current_location_entry: Any | None = None,
    reconcile: bool = False,
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
    `places` is the place tree's nodes, so every mention can be linked to
    a real id — and a garbled transcription of an obscure real spot has
    something concrete to match against instead of defaulting to a famous
    but wrong one. `current_location_entry` (the open location.yaml entry)
    focuses that list on the region the user is in.

    `reconcile` switches to whole-day mode (prompts/reconcile.md): the
    transcript is all of a day's notes, and the result replaces the day's
    record instead of adding to it.

    Returns a dict matching the journal frontmatter schema, plus a
    `summary` key the caller should pull out before writing to the vault.
    """
    client = client or anthropic.Anthropic()
    system_prompt = PROMPT_PATH.read_text(encoding="utf-8")
    if reconcile:
        system_prompt += RECONCILE_PROMPT_PATH.read_text(encoding="utf-8")

    tool_choice: ToolChoiceToolParam = {"type": "tool", "name": "record_journal_entry"}
    place_index = PlaceIndex(list(places or []))
    focus = place_index.current_place(current_location_entry)
    user_content = (
        f"Today's date: {today.isoformat()}\n\n"
        f"Current location (per location.yaml): {current_location or '(not set)'}\n\n"
        f"Already logged today (for corrections only):\n"
        f"{_format_existing_entry(existing_frontmatter)}\n\n"
        f"Current goals:\n{_format_goals(goals)}\n\n"
        f"Current itinerary:\n{_format_itinerary(itinerary)}\n\n"
        f"Known places (id: name (kind) aka aliases; indented under their parent):\n"
        f"{outline(place_index, focus)}\n\n"
        f"Transcript:\n{transcript}"
    )
    messages: list[MessageParam] = [{"role": "user", "content": user_content}]

    # The model occasionally returns a malformed tool call (an array field
    # as a JSON string). It's rare and random, so ask again once before
    # giving up rather than losing the user's note.
    error: ExtractError | None = None
    for attempt in range(1, EXTRACT_ATTEMPTS + 1):
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
        llm.record("reconcile" if reconcile else "extract", MODEL, response.usage)
        try:
            return _facts_from(response)
        except ExtractError as exc:
            error = exc
            logger.warning("extract attempt %d/%d: %s", attempt, EXTRACT_ATTEMPTS, exc)
    assert error is not None
    raise error


def _repair(facts: dict[str, Any]) -> dict[str, Any]:
    """Undo the model's known double-encoding, strictly by parsing JSON.

    The failure seen in the wild: an array field arrives as a JSON *string*,
    either of that list or of the whole intended payload (summary and all).
    Parse it; keep the result only if it is a list (for that field) or an
    object whose keys are schema fields (then it supplies the payload).
    Anything else is left as is, and _validate_shape rejects it.
    """
    schema = cast(dict[str, Any], RECORD_JOURNAL_ENTRY_TOOL["input_schema"])
    known = set(schema["properties"])
    repaired = dict(facts)
    for field in _ARRAY_FIELDS:
        value = repaired.get(field)
        if not isinstance(value, str):
            continue
        try:
            parsed = json.loads(value)
        except ValueError:
            continue
        if isinstance(parsed, list):
            repaired[field] = parsed
        elif isinstance(parsed, dict) and parsed and set(parsed) <= known:
            del repaired[field]
            repaired = {**parsed, **{k: v for k, v in repaired.items() if k not in parsed}}
        logger.warning("extract: repaired %r from a JSON string", field)
    return repaired


def _facts_from(response: Any) -> dict[str, Any]:
    for block in response.content:
        if block.type == "tool_use":
            raw = dict(block.input)
            facts = _repair(raw)
            try:
                _validate_shape(facts)
            except ExtractError:
                logger.warning(
                    "extract: malformed tool input: %.600s", json.dumps(raw, default=str)
                )
                raise
            return facts
    raise ExtractError(f"no tool_use block in response (stop_reason={response.stop_reason})")
