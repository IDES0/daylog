"""End-of-day reconcile: one pass over all of a day's notes.

Each voice note is extracted on its own the moment it arrives, so a day
told in three notes can double count ("going surfing" in the morning,
"surfed two hours" at night) and ends up with three summary fragments.
Once the day is over, this re-reads every transcript for that day
together and rebuilds the day's record in one pass — so several short
notes end up exactly as good as one long one.

Only derived data is rebuilt. Transcripts are never touched, and
bookkeeping already applied when each note arrived (itinerary, location,
deadline slips, corrections) is not re-applied. Goal progress is the one
cross-file effect: goals.yaml is adjusted by the *difference* between the
old per-note totals and the reconciled day total.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from datetime import date
from typing import Any

from daylog import extract, goals
from daylog.vault import JournalEntry

logger = logging.getLogger(__name__)

_HEADING_RE = re.compile(r"^### \d{2}:\d{2}$", re.MULTILINE)

# Bookkeeping fields: applied once at capture time, never re-applied here.
_CAPTURE_ONLY = (
    "goal_slips",
    "itinerary_changes",
    "location_change",
    "corrections",
    "other_day_notes",
)


def note_count(entry: JournalEntry) -> int:
    return len(_HEADING_RE.findall(entry.transcript))


def needs_reconcile(entry: JournalEntry) -> bool:
    """A day told in more than one note, not already reconciled since the last one."""
    return note_count(entry) > 1 and not entry.frontmatter.get("reconciled")


def _totals(progress: list[dict[str, Any]] | None) -> dict[str, float]:
    out: dict[str, float] = {}
    for item in progress or []:
        goal_id = item.get("goal_id")
        delta = item.get("delta")
        if goal_id and isinstance(delta, int | float):
            out[goal_id] = out.get(goal_id, 0.0) + float(delta)
    return out


def goal_adjustments(
    old: list[dict[str, Any]] | None, new: list[dict[str, Any]] | None
) -> list[dict[str, Any]]:
    """goal_progress-shaped deltas that move goals.yaml from old totals to new."""
    before, after = _totals(old), _totals(new)
    out = []
    for goal_id in sorted(set(before) | set(after)):
        diff = after.get(goal_id, 0.0) - before.get(goal_id, 0.0)
        if abs(diff) > 1e-9:
            out.append({"goal_id": goal_id, "delta": diff})
    return out


@dataclass
class Reconciled:
    entry_date: date
    frontmatter: dict[str, Any]
    summary: str
    goal_adjustments: list[dict[str, Any]]


def rebuild(entry: JournalEntry, facts: dict[str, Any]) -> Reconciled:
    """Turn a whole-day extraction into the day's new record."""
    facts = dict(facts)
    summary = str(facts.pop("summary", "")).strip()
    for key in _CAPTURE_ONLY:
        facts.pop(key, None)
    frontmatter = {k: v for k, v in facts.items() if v not in (None, [], "")}
    if "location" not in frontmatter and entry.frontmatter.get("location"):
        frontmatter["location"] = entry.frontmatter["location"]
    frontmatter["reconciled"] = True
    return Reconciled(
        entry_date=entry.date,
        frontmatter=frontmatter,
        summary=summary,
        goal_adjustments=goal_adjustments(
            entry.frontmatter.get("goal_progress"), frontmatter.get("goal_progress")
        ),
    )


def reconcile_day(
    entry: JournalEntry,
    itinerary_summary: list[dict[str, Any]],
    active_goals: list[dict[str, Any]],
    places: list[Any],
    location_entry: Any | None,
) -> Reconciled:
    facts = extract.extract(
        entry.transcript,
        active_goals,
        itinerary_summary,
        entry.date,
        current_location=location_entry.get("place") if location_entry else None,
        places=places,
        current_location_entry=location_entry,
        reconcile=True,
    )
    return rebuild(entry, facts)


def apply_goal_adjustments(goals_data: Any, adjustments: list[dict[str, Any]]) -> list[str]:
    applied = goals.apply_progress(goals_data, adjustments)
    return [f"{a.title} {a.delta:+g} (now {a.new_progress:g})" for a in applied]
