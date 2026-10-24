"""Day-level work: end-of-day reconcile, the day cutoff, the weekly review.

The logical day ends at DAY_CUTOFF_HOUR (default 4am), not midnight — a
voice note at 1am about the night out belongs to the day that's ending.
The scheduler (scheduler.py) reconciles at the cutoff the day that just
ended, and also catches up on any recent multi-note day it missed (the
app was down, or a late note arrived after a reconcile).
"""

from __future__ import annotations

import logging
import os
from datetime import date, datetime, timedelta

from daylog import brief, disciplines, goals, itinerary, llm, reconcile, review
from daylog.vault import Vault, VaultError

logger = logging.getLogger(__name__)

CATCH_UP_DAYS = 3


def day_cutoff_hour() -> int:
    return int(os.environ.get("DAY_CUTOFF_HOUR", "4"))


def logical_day(now: datetime, cutoff_hour: int | None = None) -> date:
    """The journal day `now` belongs to: before the cutoff, it's still yesterday."""
    cutoff = day_cutoff_hour() if cutoff_hour is None else cutoff_hour
    if now.hour < cutoff:
        return now.date() - timedelta(days=1)
    return now.date()


def run_reconcile(v: Vault, day: date, force: bool = False) -> str:
    """Reconcile one day. Returns a human-readable outcome line."""
    entry = v.read_journal_entry(day)
    if entry is None:
        return f"{day.isoformat()}: no entry."
    if not force and not reconcile.needs_reconcile(entry):
        return f"{day.isoformat()}: nothing to reconcile."

    location_data = v.read_location()
    goals_data = v.read_goals()
    graph = disciplines.Graph(v.read_disciplines())
    result = reconcile.reconcile_day(
        entry,
        itinerary.active_summary(v.read_itinerary()),
        goals.active_summary(goals_data),
        v.read_places(),
        brief.current_location(location_data),
        graph,
        disciplines.history(graph, disciplines.all_days(v, day - timedelta(days=1))),
    )
    v.rewrite_journal_entry(
        day, result.frontmatter, f"journal: {day.isoformat()} (reconciled)", summary=result.summary
    )
    goal_lines: list[str] = []
    if result.goal_adjustments:
        goal_lines = reconcile.apply_goal_adjustments(goals_data, result.goal_adjustments)
        if goal_lines:
            v.write_goals(goals_data, f"goals: reconcile {day.isoformat()}")
    adjusted = f"\nGoal totals corrected: {'; '.join(goal_lines)}" if goal_lines else ""
    return f"{day.isoformat()} reconciled ({reconcile.note_count(entry)} notes).{adjusted}"


def reconcile_recent(v: Vault, today: date) -> list[str]:
    """Reconcile the days before `today` (up to CATCH_UP_DAYS back) that need it.
    Returns one outcome line per day reconciled; a failed day is logged and skipped."""
    outcomes = []
    for offset in range(1, CATCH_UP_DAYS + 1):
        day = today - timedelta(days=offset)
        entry = v.read_journal_entry(day)
        if entry is None or not reconcile.needs_reconcile(entry):
            continue
        try:
            outcomes.append(run_reconcile(v, day))
        except Exception:
            logger.exception("reconcile failed for %s", day)
    llm.flush(v)
    return outcomes


# -- weekly review ----------------------------------------------------------


def review_hour() -> int:
    return int(os.environ.get("REVIEW_HOUR", "20"))


def weekly_plan_enabled() -> bool:
    return os.environ.get("WEEKLY_PLAN", "1") not in ("0", "false", "no")


def write_review(v: Vault, any_day: date) -> tuple[str, date, date, str] | None:
    """Write the review of `any_day`'s week (paid) and save it under reviews/.
    Returns (week label, start, end, text), or None when the week has no entries.
    Raises if the model call fails; a failed save is logged."""
    start, end = review.week_bounds(any_day)
    entries = v.read_journal_range(start, end)
    if not entries:
        return None
    goal_lines = "\n".join(
        f"- {g['id']}: {g['title']} ({g['type']}, metric {g.get('metric') or '-'})"
        for g in goals.active_summary(v.read_goals())
    )
    last = v.read_text(f"reviews/{review.week_label(start - timedelta(days=1))}.md")
    weekly_docs = v.list_docs("research/weekly")
    weekly_research = v.read_text(weekly_docs[-1]) if weekly_docs else None
    stats = review.week_stats(entries)
    graph = disciplines.Graph(v.read_disciplines())
    if graph:
        week = disciplines.totals(graph, {d: e.frontmatter for d, e in entries.items()})
        stats["by_discipline"] = disciplines.numbers(graph, week)
    review_input = review.build_input(
        entries,
        stats,
        goal_lines or "(none)",
        last,
        llm.format_usage(v),
        v.read_principles(),
        weekly_research,
    )
    try:
        text, _cost = review.write(review_input)
    finally:
        llm.flush(v)
    label = review.week_label(start)
    try:
        v.write_text(f"reviews/{label}.md", f"# Week {label}\n\n{text}\n", f"review: {label}")
    except VaultError:
        logger.exception("saving review failed")
    return label, start, end, text
