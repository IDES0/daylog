"""Proposed changes waiting for a tap: research/proposals/<date>-<slug>.yaml.

A decision (a job, a flight home, an injury) or a stale stretch of the
itinerary leaves the plan out of date. The morning research routine, or
/ripple on demand, works out what should change and writes one proposal
file; it never applies anything. The bot shows each pending proposal after
the morning brief as one card, and records what the user chose.

    decision: Accepted the NASCO internship in Austin, Oct 20 – Apr 20
    made_on: 2026-10-16
    by: routine                # or bot (/ripple)
    analysis: research/decisions/2026-10-16-nasco.md
    summary:                   # a few short lines for the card
      - Nepal course and Sri Lanka are blocked
    changes:                   # chat-proposal shape: edit itinerary | goal
      - {edit: itinerary, id: to-nepal, status: dropped, summary: Drop the Nepal flight}
    plan_draft: research/proposals/2026-10-16-nasco-plan.md   # optional
    status: pending            # applied | reviewed | skipped, set by the bot

Pure: no vault access here.
"""

from __future__ import annotations

from typing import Any

from daylog import ripple

FOLDER = "research/proposals"
PENDING = "pending"


def is_pending(data: Any) -> bool:
    return isinstance(data, dict) and data.get("status", PENDING) == PENDING


def changes_for(data: dict[str, Any], itinerary_ids: set[str], goal_ids: set[str]) -> list[Any]:
    """The proposal's edits that still point at real entries."""
    raw = [c for c in data.get("changes") or [] if isinstance(c, dict) and c.get("summary")]
    return ripple.valid_changes(raw, itinerary_ids, goal_ids)


def card_text(data: dict[str, Any], changes: list[Any]) -> str:
    lines = [f"⚡ {data.get('decision', 'A decision')}"]
    lines += [f"• {line}" for line in data.get("summary") or []]
    if changes:
        lines += ["", "Proposed changes:"]
        lines += [f"{i}. {c['summary']}" for i, c in enumerate(changes, 1)]
    if data.get("plan_draft"):
        lines += ["", "Plus a revised plan, used if you apply."]
    if data.get("analysis"):
        lines += ["", f"Full analysis: {data['analysis']}"]
    return "\n".join(lines)


def build(result: ripple.Ripple, made_on: str, analysis: str, by: str = "bot") -> dict[str, Any]:
    """A proposal file from a /ripple analysis."""
    summary = [f"{i['label']}: {i['effect'].replace('_', ' ')}" for i in result.impacts][:6]
    return {
        "decision": result.decision,
        "made_on": made_on,
        "by": by,
        "analysis": analysis,
        "summary": summary,
        "changes": result.changes,
        "status": PENDING,
    }
