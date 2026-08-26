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


def _date_reference_table(today: date, days: int = 7) -> str:
    """Explicit ISO date -> weekday-name pairs for today and the next `days` days.

    Handed to the model instead of a bare ISO date so it never has to compute
    a weekday itself — a plain `today.isoformat()` led it to miscompute the
    day of week for dates later in the brief (e.g. calling the 25th a
    Friday when it wasn't).
    """
    lines = [f"{today.isoformat()} ({today.strftime('%A')}) — today"]
    for offset in range(1, days + 1):
        day = today + timedelta(days=offset)
        lines.append(f"{day.isoformat()} ({day.strftime('%A')})")
    return "\n".join(lines)


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


def _format_checklist(checklist: Any) -> str:
    lines = []
    for item in checklist:
        status = item.get("status", "todo")
        dates = ""
        item_dates = item.get("dates")
        if item_dates and len(item_dates) > 1:
            dates = f" ({item_dates[0]}–{item_dates[-1]})"
        elif item_dates:
            dates = f" ({item_dates[0]})"
        notes = f" — {item['notes']}" if item.get("notes") else ""
        lines.append(f"    - [{status}] {item.get('item', '?')}{dates}{notes}")
    return "\n".join(lines)


def _format_surf_spots(surf_spots: Any) -> str:
    lines = []
    for s in surf_spots:
        break_type = s.get("break_type", "?")
        swell = s.get("ideal_swell_direction")
        swell_bit = f", ideal swell {swell}" if swell else ""
        wind_dir = s.get("ideal_wind_direction")
        wind_bit = f", ideal wind {wind_dir}" if wind_dir else ""
        level = s.get("level")
        level_bit = f", {level}" if level else ""
        notes = f" — {s['notes']}" if s.get("notes") else ""
        lines.append(
            f"    - {s.get('name', '?')} ({break_type}{swell_bit}{wind_bit}{level_bit}){notes}"
        )
    return "\n".join(lines)


def _format_wind_spots(wind_spots: Any) -> str:
    lines = []
    for s in wind_spots:
        wind_dir = s.get("ideal_wind_direction")
        wind_bit = f", ideal wind {wind_dir}" if wind_dir else ""
        notes = f" — {s['notes']}" if s.get("notes") else ""
        lines.append(f"    - {s.get('name', '?')} (wind spot{wind_bit}){notes}")
    return "\n".join(lines)


def _format_places(places_data: Any) -> str:
    if not places_data:
        return "(none curated yet)"
    lines = []
    for p in places_data:
        activities = ", ".join(p.get("activities", [])) or "unspecified"
        notes = f" — {p['notes']}" if p.get("notes") else ""
        tag = " [current]" if p.get("current") else ""
        lines.append(f"- {p.get('name', '?')}{tag}: {activities}{notes}")
        if p.get("checklist"):
            lines.append(_format_checklist(p["checklist"]))
        if p.get("surf_spots"):
            lines.append(_format_surf_spots(p["surf_spots"]))
        if p.get("wind_spots"):
            lines.append(_format_wind_spots(p["wind_spots"]))
    return "\n".join(lines)


def _format_profile(profile_data: Any) -> str:
    if not profile_data:
        return "(no profile set)"
    lines = [f"- {k}: {v}" for k, v in profile_data.items()]
    return "\n".join(lines)


def generate_brief(
    *,
    today: date,
    goals_data: Any,
    itinerary_data: Any,
    places_data: Any,
    location_data: Any,
    profile_data: Any = None,
    recent_journal: str,
    marine_forecast: str | None,
    wind_forecast: str | None = None,
    client: anthropic.Anthropic | None = None,
) -> str:
    client = client or anthropic.Anthropic()
    system_prompt = PROMPT_PATH.read_text(encoding="utf-8")

    current = current_location(location_data)
    location_line = current.get("place", "unknown") if current else "unknown"

    # marine_forecast may cover more than one spot (current location plus any
    # curated surf_spots nearby) — each spot's block is already labeled by
    # the caller, this just passes the combined text through.
    user_content = (
        f"Date reference (use these, don't compute weekdays yourself):\n"
        f"{_date_reference_table(today)}\n\n"
        f"Current location: {location_line}\n\n"
        f"Surfer profile:\n{_format_profile(profile_data)}\n\n"
        f"Goals:\n{_format_goals(goals_data)}\n\n"
        f"Itinerary:\n{_format_itinerary(itinerary_data)}\n\n"
        f"Curated places knowledge:\n{_format_places(places_data)}\n\n"
        f"Marine/swell forecast (current location and any nearby curated surf spots):\n"
        f"{marine_forecast or '(not coastal, or unavailable)'}\n\n"
        f"Wind forecast (current location and any nearby curated surf/wind spots):\n"
        f"{wind_forecast or '(unavailable)'}\n\n"
        f"Recent journal entries:\n{recent_journal}"
    )
    messages: list[MessageParam] = [{"role": "user", "content": user_content}]

    response = client.messages.create(
        model=MODEL,
        max_tokens=8192,
        system=system_prompt,
        tools=[WEB_SEARCH_TOOL],
        messages=messages,
    )

    search_requests = (
        response.usage.server_tool_use.web_search_requests if response.usage.server_tool_use else 0
    )
    logger.info(
        "brief usage: input=%d output=%d web_searches=%d/%d",
        response.usage.input_tokens,
        response.usage.output_tokens,
        search_requests,
        WEB_SEARCH_TOOL["max_uses"],
    )
    if response.stop_reason == "max_tokens":
        logger.warning("brief response was truncated by max_tokens")

    # The client never sees search result page content (it's opaque
    # `encrypted_content`, for the model's own use) — title/url/error_code
    # is the only visibility we get into whether search actually found
    # anything specific, so log it every time rather than only on failure.
    for block in response.content:
        if block.type != "web_search_tool_result":
            continue
        if isinstance(block.content, list):
            found = "; ".join(f"{r.title} ({r.url})" for r in block.content) or "(no results)"
            logger.info("web_search result: %s", found)
        else:
            logger.warning("web_search error: %s", block.content.error_code)

    # Citations split a response into multiple text blocks at the citation
    # boundary, mid-sentence — the blocks are contiguous, not separate
    # paragraphs, and each block's own text already carries whatever real
    # line breaks the model intended. Join with nothing, not "\n\n".
    text = "".join(block.text for block in response.content if block.type == "text")
    if not text:
        raise BriefError(f"no text in response (stop_reason={response.stop_reason})")
    return text
