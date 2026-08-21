"""Applying extracted goal facts to the live goals.yaml structure.

Pure logic — no filesystem or git access (vault.py owns that). Callers pass
in the structure from `vault.read_goals()` and this module mutates it in
place, reporting what happened so bot.py can build a reply/commit message.

Hard deadlines never move silently (SPEC): a `goal_slips` entry for a hard
goal is never applied here — it's returned as a `PendingSlip` for the
caller to confirm with the user first, then apply via
`apply_confirmed_slip` once they do. Soft goals apply immediately.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import Any


@dataclass
class AppliedProgress:
    goal_id: str
    title: str
    delta: float
    new_progress: float


@dataclass
class AppliedSlip:
    goal_id: str
    title: str
    old_date: str | None
    new_date: str
    reason: str | None


@dataclass
class PendingSlip:
    goal_id: str
    title: str
    old_date: str | None
    new_date: str
    reason: str | None


def find_goal(goals: list[Any], goal_id: str) -> Any | None:
    for goal in goals:
        if goal.get("id") == goal_id:
            return goal
    return None


def apply_progress(goals: list[Any], goal_progress: list[dict[str, Any]]) -> list[AppliedProgress]:
    """Apply each {goal_id, delta} to the matching goal's progress field, in place."""
    applied = []
    for item in goal_progress:
        goal = find_goal(goals, item.get("goal_id", ""))
        delta = item.get("delta")
        if goal is None or delta is None:
            continue
        new_progress = (goal.get("progress") or 0) + delta
        goal["progress"] = new_progress
        applied.append(
            AppliedProgress(
                goal_id=goal["id"],
                title=goal.get("title", goal["id"]),
                delta=delta,
                new_progress=new_progress,
            )
        )
    return applied


def apply_slips(
    goals: list[Any], goal_slips: list[dict[str, Any]], on: date
) -> tuple[list[AppliedSlip], list[PendingSlip]]:
    """Split goal_slips into ones applied immediately (soft) vs. held for confirmation (hard)."""
    applied: list[AppliedSlip] = []
    pending: list[PendingSlip] = []
    for item in goal_slips:
        goal = find_goal(goals, item.get("goal_id", ""))
        new_date = item.get("new_date")
        if goal is None or not new_date:
            continue
        reason = item.get("reason")
        old_date = _current_target_date(goal)

        if goal.get("type") == "hard":
            pending.append(
                PendingSlip(
                    goal_id=goal["id"],
                    title=goal.get("title", goal["id"]),
                    old_date=old_date,
                    new_date=new_date,
                    reason=reason,
                )
            )
        else:
            _write_slip(goal, old_date, new_date, reason, on)
            applied.append(
                AppliedSlip(
                    goal_id=goal["id"],
                    title=goal.get("title", goal["id"]),
                    old_date=old_date,
                    new_date=new_date,
                    reason=reason,
                )
            )
    return applied, pending


def apply_confirmed_slip(goals: list[Any], slip: PendingSlip, on: date) -> None:
    """Apply a hard-goal slip after the user has confirmed it in Telegram."""
    goal = find_goal(goals, slip.goal_id)
    if goal is None:
        return
    _write_slip(goal, slip.old_date, slip.new_date, slip.reason, on)


def _current_target_date(goal: Any) -> str | None:
    if "deadline" in goal:
        return str(goal["deadline"])
    window = goal.get("target_window")
    if window:
        return str(window[-1])
    return None


def _write_slip(
    goal: Any, old_date: str | None, new_date: str, reason: str | None, on: date
) -> None:
    """Mutate `goal` in place: move its date and append a slip_history entry.

    Which field holds the date is decided by the goal's `type`, not by
    which field happens to already be set — a hard goal always uses
    `deadline`, a soft goal always uses `target_window`, even the first
    time either is set.

    Dates are stored as real `date` objects, not strings — ruamel dumps a
    plain string that looks like a date (e.g. from extraction) as a quoted
    string, `deadline: '2026-10-15'`, unlike a hand-typed unquoted date,
    which loads as an actual `date`. Writing a real object keeps the file
    looking the same whether a date was hand-edited or bot-written.
    """
    new_date_value = date.fromisoformat(new_date)
    if goal.get("type") == "hard":
        goal["deadline"] = new_date_value
    else:
        window = goal.get("target_window")
        if window:
            window[-1] = new_date_value
        else:
            goal["target_window"] = [new_date_value, new_date_value]

    history = goal.get("slip_history")
    if history is None:
        history = []
        goal["slip_history"] = history
    entry: dict[str, Any] = {
        "from": date.fromisoformat(old_date) if old_date else None,
        "to": new_date_value,
        "on": on,
    }
    if reason:
        entry["reason"] = reason
    history.append(entry)
