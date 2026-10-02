"""Telegram side of the chat assistant, and the persistent journaling menu.

Routing (see bot.handle_text / handle_voice):
- a voice note is a journal entry for today (or the date picked first);
- typed text after picking a day under "📝 Log" is a journal entry for that day;
- the other menu buttons run the matching command;
- any other typed text goes to the assistant.

Conversation history lives in bot_data (single-user bot), so the morning
brief and weekly review can be added to it from scheduled jobs — a reply
to the brief then has the brief as context.
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from datetime import date, datetime
from typing import Any

from anthropic.types import MessageParam
from telegram import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
    ReplyKeyboardMarkup,
    Update,
)
from telegram.ext import ContextTypes

from daylog import chat, edits, itinerary, llm, rank_flow, rankings, research_flow
from daylog.places import PlaceIndex
from daylog.tg import allowed_user_id, chunks, is_authorized, tz, vault
from daylog.vault import Vault, VaultError

logger = logging.getLogger(__name__)

HISTORY_KEY = "chat_history"
_PROPOSALS_KEY = "chat_proposals"

BTN_SURF = "🌊 Surf"
BTN_FLY = "🪂 Fly"
BTN_BRIEF = "☀️ Brief"
BTN_LOG = "📝 Log"
BTN_PLAN = "🗺 Plan"
BTN_MORE = "⋯ More"

BTN_STATUS = "📌 Status"
BTN_UPCOMING = "📅 Upcoming"
BTN_TRAIL = "🧭 Trail"
BTN_RANKINGS = "🏆 Rankings"
BTN_WISHLIST = "⭐ Wishlist"
BTN_REVIEW = "📊 Review"
BTN_UNDO = "↩️ Undo"
BTN_USAGE = "💸 Usage"
BTN_BACK = "⬅ Back"

_PLACEHOLDER = "Ask anything — voice notes log today"

MENU = ReplyKeyboardMarkup(
    [[BTN_SURF, BTN_FLY, BTN_BRIEF], [BTN_LOG, BTN_PLAN, BTN_MORE]],
    resize_keyboard=True,
    is_persistent=True,
    input_field_placeholder=_PLACEHOLDER,
)

# The second page, swapped in by "⋯ More". Everything here is also a typed
# command; commands that need an argument (/place, /rank, /research, /want)
# stay typed, or just ask the assistant.
MORE_MENU = ReplyKeyboardMarkup(
    [
        [BTN_STATUS, BTN_UPCOMING, BTN_TRAIL],
        [BTN_RANKINGS, BTN_WISHLIST, BTN_REVIEW],
        [BTN_UNDO, BTN_USAGE],
        [BTN_BACK],
    ],
    resize_keyboard=True,
    is_persistent=True,
    input_field_placeholder=_PLACEHOLDER,
)


def history(context: ContextTypes.DEFAULT_TYPE) -> list[MessageParam]:
    stored: list[MessageParam] = context.bot_data.setdefault(HISTORY_KEY, [])
    return stored


def remember(context: ContextTypes.DEFAULT_TYPE, label: str, text: str) -> None:
    """Add something the bot sent on its own (brief, review) to the conversation."""
    hist = history(context)
    hist.extend(
        [
            {"role": "user", "content": f"[{label}]"},
            {"role": "assistant", "content": text},
        ]
    )
    context.bot_data[HISTORY_KEY] = chat.trim_history(hist)


async def handle_chat(message: Message, context: ContextTypes.DEFAULT_TYPE, text: str) -> None:
    v = vault()
    await context.bot.send_chat_action(chat_id=message.chat_id, action="typing")
    try:
        result = await asyncio.to_thread(
            chat.respond,
            v,
            list(history(context)),
            text,
            datetime.now(tz()),
            allow_web=llm.can_spend(v),
        )
    except Exception:
        logger.exception("chat failed")
        await message.reply_text("Something went wrong answering that — check bot logs.")
        return
    context.bot_data[HISTORY_KEY] = result.new_history
    for chunk in chunks(result.text):
        await message.reply_text(chunk, reply_markup=MENU)
    for proposal in result.proposals:
        await _send_proposal(message, context, proposal)
    for job in result.research_jobs:
        context.application.create_task(_run_research_job(context, message.chat_id, job))
    llm.flush(v)


async def _send_proposal(
    message: Message, context: ContextTypes.DEFAULT_TYPE, proposal: dict[str, Any]
) -> None:
    token = uuid.uuid4().hex[:10]
    context.bot_data.setdefault(_PROPOSALS_KEY, {})[token] = proposal
    keyboard = InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("Confirm", callback_data=f"chatitin:ok:{token}"),
                InlineKeyboardButton("No", callback_data=f"chatitin:no:{token}"),
            ]
        ]
    )
    icon = _ICONS.get(str(proposal.get("edit")), "✏️")
    await message.reply_text(f"{icon} {proposal.get('summary', 'Change')}", reply_markup=keyboard)


_ICONS = {
    "itinerary": "🗺",
    "place": "📍",
    "merge": "🔗",
    "delete_place": "🗑",
    "journal": "📓",
    "location": "🧭",
    "goal": "🎯",
    "focus": "🔭",
}


def apply_edit(v: Vault, proposal: dict[str, Any], today: date) -> str:
    """Apply one confirmed chat proposal to the vault. Returns what changed."""
    # "edit" names the kind of change; "kind" inside a place edit is the
    # place's own kind (surf_spot, food, ...), so the two must not share a key.
    kind = proposal.get("edit", "itinerary")
    if kind == "itinerary":
        data = v.read_itinerary()
        outcome = apply_proposal(data, proposal, today)
        v.write_itinerary(data, f"itinerary: {proposal.get('place') or proposal.get('id')}")
        return outcome
    if kind == "place":
        files = v.read_place_files()
        changed, outcome = edits.edit_place(files, proposal)
        v.write_place_files(files, changed, f"places: {outcome}")
        return outcome
    if kind == "delete_place":
        files = v.read_place_files()
        place_id = str(proposal.get("place_id"))
        changed, outcome = edits.delete_place(files, place_id, chat.linked_days(v, place_id, today))
        v.write_place_files(files, changed, f"places: {outcome}")
        return outcome
    if kind == "merge":
        source, target = str(proposal.get("source_id")), str(proposal.get("target_id"))
        days = chat.linked_days(v, source, today)
        files = v.read_place_files()
        changed, outcome = edits.merge_places(files, source, target)
        v.write_place_files(files, changed, f"places: {outcome}")
        for day in days:
            entry = v.read_journal_entry(day)
            if entry is not None and edits.relink_journal(entry.frontmatter, source, target):
                v.rewrite_journal_entry(
                    day, entry.frontmatter, f"journal: {day.isoformat()} relink {source}"
                )
        current = v.read_yaml(rank_flow.RANKINGS_FILE, {})
        if edits.relink_rankings(current, source, target):
            v.write_yaml(
                rank_flow.RANKINGS_FILE, current, f"rankings: merge {source} into {target}"
            )
        return outcome
    if kind == "journal":
        day = date.fromisoformat(str(proposal.get("date")))
        entry = v.read_journal_entry(day)
        if entry is None:
            raise edits.EditError(f"no journal entry for {day}")
        known = {n["id"] for n in v.read_places()}
        outcome = edits.edit_journal(entry.frontmatter, proposal, known)
        v.rewrite_journal_entry(day, entry.frontmatter, f"journal: {day.isoformat()} (edit)")
        return f"{day.isoformat()}: {outcome}"
    if kind == "location":
        location = v.read_location()
        outcome = edits.edit_location(location, proposal)
        v.write_yaml("location.yaml", location, f"location: {outcome}")
        return outcome
    if kind == "goal":
        data = v.read_goals()
        outcome = edits.edit_goal(data, proposal, today)
        v.write_goals(data, f"goals: {outcome}")
        return outcome
    if kind == "focus":
        profile = v.read_profile() or {}
        outcome = edits.edit_focus(profile, proposal)
        v.write_profile(profile, f"profile: {outcome}")
        return outcome
    raise edits.EditError(f"unknown edit kind {kind!r}")


def apply_proposal(itinerary_data: Any, proposal: dict[str, Any], on: Any) -> str:
    """Apply a confirmed chat proposal. The tap *is* the confirmation, so a hard
    date is applied directly rather than asked about a second time."""
    change = {
        k: proposal[k]
        for k in ("id", "place", "place_id", "status", "new_date", "why", "notes")
        if proposal.get(k)
    }
    applied, pending = itinerary.apply_itinerary_changes(itinerary_data, [change], on=on)
    for p in pending:
        itinerary.apply_confirmed_change(itinerary_data, p, on=on)
    return "; ".join([a.summary for a in applied] + [f"{p.place}: {p.new_date}" for p in pending])


async def handle_proposal_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_authorized(update):
        return
    query = update.callback_query
    assert query is not None and query.data is not None
    await query.answer()
    _, action, token = query.data.split(":", 2)
    proposal = context.bot_data.get(_PROPOSALS_KEY, {}).pop(token, None)
    if proposal is None:
        await query.edit_message_text("This proposal expired (the bot restarted).")
        return
    if action == "no":
        await query.edit_message_text(f"Left as is: {proposal.get('summary')}")
        return
    v = vault()
    try:
        outcome = apply_edit(v, proposal, datetime.now(tz()).date())
    except edits.EditError as exc:
        await query.edit_message_text(f"Couldn't apply that: {exc}")
        return
    except VaultError:
        logger.exception("applying chat proposal failed")
        await query.edit_message_text("Saving failed — check bot logs.")
        return
    await query.edit_message_text(f"Done: {outcome or proposal.get('summary')}")


async def _run_research_job(
    context: ContextTypes.DEFAULT_TYPE, chat_id: int, job: dict[str, Any]
) -> None:
    try:
        if job.get("job") == "rank":
            index = PlaceIndex(vault().read_places())
            node = index.get(str(job.get("place")))
            category = rankings.category_for(node.get("kind")) if node else None
            if node is None or category is None:
                await context.bot.send_message(chat_id=chat_id, text="That place can't be ranked.")
                return
            await rank_flow.ask_to_rank_chat(
                context, chat_id, node["id"], category, str(node.get("name"))
            )
            return
        if job.get("job") == "explore":
            await research_flow.arrive(context, chat_id, force=True)
            return
        v = vault()
        index = PlaceIndex(v.read_places())
        name = str(job.get("place") or "")
        matches = index.resolve(name) if name else []
        place_id = matches[0] if matches else None
        label = index.path_name(place_id) if place_id else name
        path, text = await research_flow.write_dossier(v, place_id, label)
        for chunk in chunks(f"{text}\n\n(saved to {path})"):
            await context.bot.send_message(chat_id=chat_id, text=chunk)
    except Exception:
        logger.exception("research job from chat failed: %s", job)
        await context.bot.send_message(chat_id=chat_id, text="That research job failed.")


async def send_menu(context: ContextTypes.DEFAULT_TYPE, text: str) -> None:
    await context.bot.send_message(chat_id=allowed_user_id(), text=text, reply_markup=MENU)


async def undo_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/undo — pick one of the bot's recent changes to revert."""
    if not is_authorized(update):
        return
    message = update.message
    assert message is not None
    changes = vault().recent_changes()
    if not changes:
        await message.reply_text("Nothing to undo.")
        return
    keyboard = InlineKeyboardMarkup(
        [
            [InlineKeyboardButton(subject[:60], callback_data=f"undo:{sha}")]
            for sha, subject in changes
        ]
    )
    await message.reply_text("Undo which change? (newest first)", reply_markup=keyboard)


async def handle_undo_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_authorized(update):
        return
    query = update.callback_query
    assert query is not None and query.data is not None
    await query.answer()
    sha = query.data.split(":", 1)[1]
    try:
        vault().revert(sha)
    except VaultError as exc:
        await query.edit_message_text(f"Couldn't undo that: {exc}")
        return
    await query.edit_message_text(f"Undone ({sha}).")
