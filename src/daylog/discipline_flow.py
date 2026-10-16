"""Telegram side of the discipline graph: logging, fix-it buttons, /disciplines.

At log time a vague sport ("foiled") is narrowed to the usual kind for that
spot and the reply offers one tap to change it; a sport the graph doesn't
know yet is added only after the user taps Add. /disciplines shows the tree
with sessions, states, goals and what each locked one still needs. The brief
and the planner get the same picture as text.
"""

from __future__ import annotations

import logging
import uuid
from datetime import date, datetime

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Message, Update
from telegram.ext import ContextTypes

from daylog import disciplines
from daylog.disciplines import Fix, Graph, Guess, Proposal, guess_note, prepare
from daylog.tg import chunks, is_authorized, tz, vault
from daylog.vault import VaultError

# The logic lives in disciplines.py (shared with the web app); re-exported here.
__all__ = ["Fix", "Proposal", "guess_note", "prepare"]

logger = logging.getLogger(__name__)

_PENDING_FIX_KEY = "discipline_fixes"
_PENDING_NEW_KEY = "discipline_new"


async def after_entry(
    message: Message,
    context: ContextTypes.DEFAULT_TYPE,
    graph: Graph,
    entry_date: date,
    guesses: list[Guess],
    proposals: list[Proposal],
) -> None:
    """Fix-it buttons for each guess, an Add card for each new sport."""
    assert context.user_data is not None
    for guess in guesses:
        others = [o for o in guess.options if o != guess.chosen]
        if not others:
            continue
        token = uuid.uuid4().hex[:12]
        context.user_data.setdefault(_PENDING_FIX_KEY, {})[token] = Fix(
            entry_date, guess.index, guess.said
        )
        buttons = [
            InlineKeyboardButton(graph.name(o), callback_data=f"disc:fix:{token}:{o}")
            for o in others
        ]
        await message.reply_text(
            f"Not {graph.name(guess.chosen)}? Tap what it was.",
            reply_markup=InlineKeyboardMarkup(
                [buttons[i : i + 3] for i in range(0, len(buttons), 3)]
            ),
        )
    for proposal in proposals:
        token = uuid.uuid4().hex[:12]
        context.user_data.setdefault(_PENDING_NEW_KEY, {})[token] = proposal
        under = ", ".join(graph.name(p) for p in proposal.parents if p in graph) or "the top level"
        await message.reply_text(
            f"New discipline: {proposal.name}, under {under}. Add it?",
            reply_markup=InlineKeyboardMarkup(
                [
                    [
                        InlineKeyboardButton(
                            "Add (pursuing)", callback_data=f"disc:add:{token}:active"
                        ),
                        InlineKeyboardButton("Add (want)", callback_data=f"disc:add:{token}:want"),
                        InlineKeyboardButton("No", callback_data=f"disc:no:{token}"),
                    ]
                ]
            ),
        )


async def handle_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_authorized(update):
        return
    query = update.callback_query
    assert query is not None and query.data is not None
    await query.answer()
    assert context.user_data is not None
    parts = query.data.split(":")
    action, token = parts[1], parts[2]
    v = vault()

    if action == "fix":
        fix: Fix | None = context.user_data.get(_PENDING_FIX_KEY, {}).pop(token, None)
        if fix is None:
            await query.edit_message_text("This has expired or was already handled.")
            return
        chosen = parts[3]
        try:
            fixed = disciplines.apply_fix(v, fix, chosen)
        except VaultError:
            logger.exception("discipline fix commit failed")
            await query.edit_message_text("The fix didn't save — check bot logs.")
            return
        if not fixed:
            await query.edit_message_text(
                "That entry changed since — nothing was edited. Say the correction instead."
            )
            return
        graph = Graph(v.read_disciplines())
        await query.edit_message_text(f"Fixed — logged as {graph.name(chosen)}.")
        return

    proposal: Proposal | None = context.user_data.get(_PENDING_NEW_KEY, {}).pop(token, None)
    if proposal is None:
        await query.edit_message_text("This has expired or was already handled.")
        return
    if action == "no":
        await query.edit_message_text(
            f"Left {proposal.name} out of the disciplines; the day still logs it."
        )
        return
    status = parts[3] if len(parts) > 3 else "active"
    try:
        added = disciplines.add_discipline(v, proposal, status)
    except VaultError:
        logger.exception("disciplines commit failed")
        await query.edit_message_text("Adding it didn't save — check bot logs.")
        return
    if not added:
        await query.edit_message_text(f"{proposal.name} is already a discipline.")
        return
    await query.edit_message_text(f"Added {proposal.name} to the disciplines.")


async def disciplines_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/disciplines [days] — the tree with sessions, states, goals and locks."""
    if not is_authorized(update):
        return
    message = update.message
    assert message is not None
    args = context.args or []
    try:
        days = int(args[0]) if args else disciplines.RECENT_DAYS
    except ValueError:
        await message.reply_text("Usage: /disciplines or /disciplines 90")
        return
    v = vault()
    today = datetime.now(tz()).date()
    graph, recent, all_time = disciplines.snapshot(v, today, days)
    if not graph:
        await message.reply_text("No disciplines.yaml in the vault yet.")
        return
    text = disciplines.tree(graph, v.read_goals(), recent, all_time, days)
    for chunk in chunks(text):
        await message.reply_text(chunk)
