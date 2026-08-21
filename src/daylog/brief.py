"""Morning brief: gather vault context, one Claude call with web_search, plain text back.

Unlike extract.py's forced single-tool-use call, this is meant to be read
directly by the user — no structured output, no tool_choice forcing.
Claude decides when to call web_search itself; the final text response
(after any searches) is the brief.
"""

from __future__ import annotations

import logging
import re
from datetime import date, timedelta
from pathlib import Path
from typing import Any

import anthropic
from anthropic.types import MessageParam, WebSearchTool20260209Param

from daylog.vault import Vault

logger = logging.getLogger(__name__)

MODEL = "claude-sonnet-5"
PROMPT_PATH = Path(__file__).parent / "prompts" / "brief.md"

WEB_SEARCH_TOOL: WebSearchTool20260209Param = {
    "type": "web_search_20260209",
    "name": "web_search",
    "max_uses": 5,
}


class BriefError(RuntimeError):
    pass


def current_location(location_data: Any) -> Any | None:
    """The location entry with to: null (current), or the most recent if none is open."""
    for entry in location_data:
        if entry.get("to") is None:
            return entry
    return location_data[-1] if location_data else None


_HEADING_RE = re.compile(r"^### \d{2}:\d{2}$")


def recent_journal_summaries(vault: Vault, today: date, days: int = 5) -> str:
    """One line per day, stripped of the `### HH:MM` per-entry headings.

    A day with more than one entry (see vault.py's append behavior) has
    more than one summary line internally — flattened here into a single
    space-joined line, since the brief wants a quick "what happened that
    day," not a re-rendering of the journal file's own structure.
    """
    lines = []
    for offset in range(1, days + 1):
        day = today - timedelta(days=offset)
        entry = vault.read_journal_entry(day)
        if entry is None or not entry.summary:
            continue
        clean = " ".join(
            line.strip()
            for line in entry.summary.splitlines()
            if line.strip() and not _HEADING_RE.match(line.strip())
        )
        if clean:
            lines.append(f"{day.isoformat()}: {clean}")
    return "\n".join(lines) if lines else "(no recent journal entries)"


def _format_goals(goals_data: Any) -> str:
    if not goals_data:
        return "(none)"
    lines = []
    for g in goals_data:
        if g.get("status") == "dropped":
            continue
        bits = []
        if g.get("metric"):
            bits.append(f"{g.get('progress', 0):g} {g['metric']}")
        if g.get("type") == "hard" and g.get("deadline"):
            bits.append(f"deadline {g['deadline']}")
        elif g.get("target_window") and g["target_window"][-1]:
            bits.append(f"target {g['target_window'][-1]}")
        if g.get("notes"):
            bits.append(str(g["notes"]))
        detail = f" — {', '.join(bits)}" if bits else ""
        lines.append(f"- {g.get('title', g['id'])} ({g.get('type', 'soft')}){detail}")
    return "\n".join(lines) if lines else "(none)"


def _format_itinerary(itinerary_data: Any) -> str:
    if not itinerary_data:
        return "(no itinerary entries yet)"
    lines = []
    for e in itinerary_data:
        if e.get("status") == "dropped":
            continue
        date_field = None
        if e.get("type") == "hard" and e.get("deadline"):
            date_field = f"deadline {e['deadline']}"
        elif e.get("target_window") and e["target_window"][-1]:
            date_field = f"target {e['target_window'][-1]}"
        bits = [b for b in [date_field, e.get("notes")] if b]
        detail = f" — {', '.join(str(b) for b in bits)}" if bits else ""
        lines.append(
            f"- {e.get('place', e['id'])} [{e.get('status', 'candidate')}] "
            f"({e.get('type', 'soft')}){detail}"
        )
    return "\n".join(lines) if lines else "(none)"


def _format_places(places_data: Any) -> str:
    if not places_data:
        return "(none curated yet)"
    lines = []
    for p in places_data:
        activities = ", ".join(p.get("activities", [])) or "unspecified"
        notes = f" — {p['notes']}" if p.get("notes") else ""
        lines.append(f"- {p.get('name', '?')}: {activities}{notes}")
    return "\n".join(lines)


def generate_brief(
    *,
    today: date,
    goals_data: Any,
    itinerary_data: Any,
    places_data: Any,
    location_data: Any,
    recent_journal: str,
    marine_forecast: str | None,
    client: anthropic.Anthropic | None = None,
) -> str:
    client = client or anthropic.Anthropic()
    system_prompt = PROMPT_PATH.read_text(encoding="utf-8")

    current = current_location(location_data)
    location_line = current.get("place", "unknown") if current else "unknown"

    user_content = (
        f"Today's date: {today.isoformat()}\n\n"
        f"Current location: {location_line}\n\n"
        f"Goals:\n{_format_goals(goals_data)}\n\n"
        f"Itinerary:\n{_format_itinerary(itinerary_data)}\n\n"
        f"Curated places knowledge:\n{_format_places(places_data)}\n\n"
        f"Marine/swell forecast for current location:\n"
        f"{marine_forecast or '(not coastal, or unavailable)'}\n\n"
        f"Recent journal entries:\n{recent_journal}"
    )
    messages: list[MessageParam] = [{"role": "user", "content": user_content}]

    response = client.messages.create(
        model=MODEL,
        max_tokens=4096,
        system=system_prompt,
        tools=[WEB_SEARCH_TOOL],
        messages=messages,
    )

    logger.info(
        "brief usage: input=%d output=%d",
        response.usage.input_tokens,
        response.usage.output_tokens,
    )

    # Citations split a response into multiple text blocks at the citation
    # boundary, mid-sentence — the blocks are contiguous, not separate
    # paragraphs, and each block's own text already carries whatever real
    # line breaks the model intended. Join with nothing, not "\n\n".
    text = "".join(block.text for block in response.content if block.type == "text")
    if not text:
        raise BriefError(f"no text in response (stop_reason={response.stop_reason})")
    return text
