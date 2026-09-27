"""Scheduled and on-demand day-level jobs: end-of-day reconcile, the day cutoff.

The logical day ends at DAY_CUTOFF_HOUR (default 4am), not midnight — a
voice note at 1am about the night out belongs to the day that's ending.
The reconcile job runs at the cutoff for the day that just ended, and
also catches up on any recent multi-note day it missed (the bot was down,
or a late note arrived after a reconcile).
"""

from __future__ import annotations

import logging
import os
from datetime import date, datetime, timedelta

from telegram import Update
from telegram.ext import ContextTypes

from daylog import brief, goals, itinerary, llm, reconcile
from daylog.tg import allowed_user_id, is_authorized, tz, vault
from daylog.vault import Vault

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
    result = reconcile.reconcile_day(
        entry,
        itinerary.active_summary(v.read_itinerary()),
        goals.active_summary(goals_data),
        v.read_places(),
        brief.current_location(location_data),
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


async def reconcile_job(context: ContextTypes.DEFAULT_TYPE) -> None:
    """Runs at the day cutoff: the day that just ended, plus any recent misses."""
    v = vault()
    today = logical_day(datetime.now(tz()))
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
    # Quiet unless something changed that the user would want to know about.
    notable = [o for o in outcomes if "corrected" in o]
    if notable:
        await context.bot.send_message(chat_id=allowed_user_id(), text="\n".join(notable))


async def reconcile_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/reconcile [YYYY-MM-DD] — rebuild a day from all its notes now."""
    if not is_authorized(update):
        return
    message = update.message
    assert message is not None
    args = context.args or []
    day = logical_day(datetime.now(tz()))
    if args:
        try:
            day = date.fromisoformat(args[0])
        except ValueError:
            await message.reply_text("Usage: /reconcile or /reconcile 2026-09-25")
            return
    await message.reply_text(f"Reconciling {day.isoformat()}...")
    try:
        outcome = run_reconcile(vault(), day, force=True)
    except Exception:
        logger.exception("manual reconcile failed for %s", day)
        await message.reply_text("Reconcile failed — check bot logs. Nothing was changed.")
        return
    await message.reply_text(outcome)
