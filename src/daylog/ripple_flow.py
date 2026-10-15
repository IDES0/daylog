"""Telegram side of the ripple: a decision → its impact, workarounds and one card.

Triggered by a voice note whose extraction names a decision, or by
`/ripple <decision>`. The analysis is saved to research/decisions/ (the
research routines read that folder), sent as a message, and followed by
one card: apply every proposed change, go through them one by one (as
ordinary chat confirm cards), or skip. After applying, a button offers to
redo the plan with the decision in it.
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from datetime import datetime
from typing import Any

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Message, Update
from telegram.ext import ContextTypes

from daylog import chat_flow, edits, itinerary, llm, plan_flow, ripple
from daylog.tg import chunks, is_authorized, tz, vault
from daylog.vault import Vault, VaultError

logger = logging.getLogger(__name__)

_RIPPLES_KEY = "ripples"
RIPPLE_ESTIMATE_USD = 0.50
PLAN_CHARS = 6000


def _itinerary_lines(itin: Any) -> list[str]:
    lines = []
    for e in itin or []:
        if e.get("status") in ("done", "dropped"):
            continue
        when = itinerary.current_date(e)
        window = e.get("target_window")
        if isinstance(window, list) and len(window) == 2:
            when = f"{window[0]} – {window[1]}"
        notes = f" — {e['notes']}" if e.get("notes") else ""
        lines.append(
            f"- {e['id']}: {e.get('place')} ({e.get('type', 'soft')}, {e.get('status')}"
            f"{', ' + str(when) if when else ''}){notes}"
        )
    return lines


def build_context(v: Vault, now: datetime, decision: str) -> str:
    plan = plan_flow.latest_plan(v) or "(no plan saved)"
    return (
        f"The decision, in their words:\n{decision}\n\n"
        f"Full itinerary (active entries):\n"
        f"{chr(10).join(_itinerary_lines(v.read_itinerary())) or '(empty)'}\n\n"
        f"{plan_flow.build_context(v, now)}\n\n"
        f"Their current plan:\n{plan[:PLAN_CHARS]}"
    )


def analyze(v: Vault, now: datetime, decision: str) -> tuple[ripple.Ripple, str]:
    """Run the analysis, keep only edits to real entries, save the decision file."""
    result = ripple.run(build_context(v, now, decision))
    result.changes = ripple.valid_changes(
        result.changes,
        {str(e["id"]) for e in v.read_itinerary() or []},
        {str(g["id"]) for g in v.read_goals() or []},
    )
    path = f"research/decisions/{now.date().isoformat()}-{ripple.slug(result.decision)}.md"
    v.write_text(path, ripple.render(result, now.date()), f"decisions: {result.decision}")
    return result, path


def _keyboard(token: str, n: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton(f"Apply all {n}", callback_data=f"ripple:all:{token}")],
            [
                InlineKeyboardButton("One by one", callback_data=f"ripple:each:{token}"),
                InlineKeyboardButton("Skip", callback_data=f"ripple:no:{token}"),
            ],
        ]
    )


async def run_for(context: ContextTypes.DEFAULT_TYPE, chat_id: int, decision: str) -> None:
    v = vault()
    if not llm.can_spend(v, RIPPLE_ESTIMATE_USD):
        await context.bot.send_message(
            chat_id=chat_id,
            text=f"Noted the decision ({decision}), but the monthly budget is spent, "
            "so I didn't trace what it changes. /usage has the numbers.",
        )
        return
    await context.bot.send_message(
        chat_id=chat_id, text=f"⚡ Tracing what this changes: {decision}"
    )
    try:
        result, path = await asyncio.to_thread(analyze, v, datetime.now(tz()), decision)
    except Exception:
        logger.exception("ripple failed")
        await context.bot.send_message(
            chat_id=chat_id, text="Couldn't trace that decision — check bot logs."
        )
        return
    finally:
        llm.flush(v)
    text = ripple.render(result, datetime.now(tz()).date(), heading="⚡").replace("**", "")
    for chunk in chunks(f"{text}\nSaved to {path}"):
        await context.bot.send_message(chat_id=chat_id, text=chunk)
    chat_flow.remember(context, "The bot traced a decision", text)
    if not result.changes:
        await context.bot.send_message(chat_id=chat_id, text="No itinerary or goal edits needed.")
        return
    token = uuid.uuid4().hex[:10]
    context.bot_data.setdefault(_RIPPLES_KEY, {})[token] = result.changes
    listing = "\n".join(f"{i}. {c['summary']}" for i, c in enumerate(result.changes, 1))
    await context.bot.send_message(
        chat_id=chat_id,
        text=f"Proposed changes:\n{listing}",
        reply_markup=_keyboard(token, len(result.changes)),
    )


def apply_all(v: Vault, changes: list[dict[str, Any]], today: Any) -> tuple[list[str], list[str]]:
    done, failed = [], []
    for change in changes:
        try:
            done.append(chat_flow.apply_edit(v, change, today) or change["summary"])
        except (edits.EditError, ValueError) as exc:
            failed.append(f"{change['summary']}: {exc}")
        except VaultError:
            logger.exception("applying a ripple change failed")
            failed.append(f"{change['summary']}: saving failed")
    return done, failed


_REPLAN = InlineKeyboardMarkup(
    [[InlineKeyboardButton("🗺 Redo the plan with this", callback_data="ripple:plan:-")]]
)


async def handle_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_authorized(update):
        return
    query = update.callback_query
    assert query is not None and query.data is not None
    await query.answer()
    _, action, token = query.data.split(":", 2)
    chat_id = query.message.chat.id if query.message else None
    if action == "plan":
        await query.edit_message_reply_markup(reply_markup=None)
        if chat_id is not None:
            await plan_flow.make_plan(context, chat_id)
        return
    changes = context.bot_data.get(_RIPPLES_KEY, {}).pop(token, None)
    if changes is None:
        await query.edit_message_text("These changes expired (the bot restarted).")
        return
    if action == "no":
        await query.edit_message_text("Left everything as is.")
        return
    if action == "each":
        await query.edit_message_reply_markup(reply_markup=None)
        if isinstance(query.message, Message):
            for change in changes:
                await chat_flow.send_proposal(query.message, context, change)
        return
    done, failed = await asyncio.to_thread(apply_all, vault(), changes, datetime.now(tz()).date())
    lines = [f"Applied {len(done)}:", *[f"• {d}" for d in done]]
    if failed:
        lines += [f"Didn't apply {len(failed)}:", *[f"• {f}" for f in failed]]
    await query.edit_message_text("\n".join(lines), reply_markup=_REPLAN)


async def ripple_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/ripple <decision> — trace a decision through the itinerary, goals and plan."""
    if not is_authorized(update):
        return
    message = update.message
    assert message is not None
    decision = " ".join(context.args or []).strip()
    if not decision:
        await message.reply_text("Usage: /ripple I accepted the job in Austin from Oct 20")
        return
    context.application.create_task(run_for(context, message.chat_id, decision))
