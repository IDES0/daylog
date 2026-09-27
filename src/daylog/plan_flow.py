"""Telegram side of the planner: /plan, option buttons, applying a choice.

The plan is saved to plans/YYYY-MM-DD.md and added to the chat history,
so "make option B a week shorter" in chat has the plan as context.
Choosing an option applies its legs as planned itinerary windows and
records the choice in the plan file (the brief reads the latest one).
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta
from typing import Any

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import ContextTypes

from daylog import brief, chat_flow, goals, itinerary, llm, planner, trail, wishlist_flow
from daylog.places import PlaceIndex
from daylog.tg import allowed_user_id, chunks, is_authorized, tz, vault
from daylog.vault import Vault, VaultError

logger = logging.getLogger(__name__)

_PLANS_KEY = "plans"
PLAN_ESTIMATE_USD = 0.60


def build_context(v: Vault, now: datetime) -> str:
    today = now.date()
    index = PlaceIndex(v.read_places())
    location_data = v.read_location()
    node = index.current_place(brief.current_location(location_data))
    where = index.path_name(node["id"]) if node else "unknown"

    goal_lines = []
    for g in v.read_goals():
        if g.get("status", "active") != "active":
            continue
        target = goals.current_target_date(g)
        progress = f", progress {g.get('progress', 0):g} {g['metric']}" if g.get("metric") else ""
        when = f", {'deadline' if g.get('type') == 'hard' else 'target'} {target}" if target else ""
        notes = f" — {g['notes']}" if g.get("notes") else ""
        goal_lines.append(
            f"- {g['id']}: {g.get('title')} ({g.get('type', 'soft')}{progress}{when}){notes}"
        )

    itin = v.read_itinerary()
    hard = [
        f"- {e.get('place')}: {itinerary.current_date(e)}"
        for e in itin
        if e.get("type") == "hard" and e.get("status") not in ("done", "dropped")
    ]
    wish_blocks = []
    dossiers = wishlist_flow.dossiers_for(v, itin)
    for e in wishlist_flow.active_entries(itin):
        label = str(e.get("place", e["id"]))
        head = (
            f"### {label} (itinerary_id {e['id']}, place_id {e.get('place_id', '-')}, "
            f"status {e.get('status')}, for {', '.join(e.get('why') or []) or '-'})"
        )
        wish_blocks.append(head + "\n" + (dossiers.get(label, "(not researched yet)")[:4000]))

    start = today - timedelta(days=30)
    entries = v.read_journal_range(start, today)
    trail_text = trail.format_trail(
        trail.build(
            location_data, {d: e.frontmatter for d, e in entries.items()}, {}, start, today
        ),
        index,
    )
    recent = brief.recent_journal_summaries(v, today + timedelta(days=1), days=7)
    profile = v.read_profile() or {}
    profile_lines = (
        [f"- {k}: {val}" for k, val in profile.items()] if isinstance(profile, dict) else []
    )
    return (
        f"Date reference:\n{brief._date_reference_table(today, days=60)}\n\n"
        f"Currently in: {where}\n\n"
        f"Profile:\n{chr(10).join(profile_lines) or '(none)'}\n\n"
        f"Goals:\n{chr(10).join(goal_lines) or '(none)'}\n\n"
        f"Hard deadlines:\n{chr(10).join(hard) or '(none recorded)'}\n\n"
        f"Last 30 days:\n{trail_text}\n\n"
        f"Recent journal:\n{recent}\n\n"
        f"Wishlist with research:\n\n{chr(10).join(wish_blocks) or '(empty)'}"
    )


def _keyboard(plan_key: str, plan: planner.Plan) -> InlineKeyboardMarkup:
    rows = [
        [
            InlineKeyboardButton(
                f"Go with {chr(65 + i)}: {str(o.get('label'))[:30]}",
                callback_data=f"plan:pick:{plan_key}:{i}",
            )
        ]
        for i, o in enumerate(plan.options)
    ]
    rows.append(
        [
            InlineKeyboardButton(
                "Not yet — I'll adjust in chat", callback_data=f"plan:hold:{plan_key}:0"
            )
        ]
    )
    return InlineKeyboardMarkup(rows)


async def make_plan(context: ContextTypes.DEFAULT_TYPE, chat_id: int) -> None:
    v = vault()
    if not llm.can_spend(v, PLAN_ESTIMATE_USD):
        await context.bot.send_message(
            chat_id=chat_id, text="Monthly budget reached — no planning run."
        )
        return
    now = datetime.now(tz())
    await context.bot.send_message(
        chat_id=chat_id, text="Planning the next few weeks — a minute or two..."
    )
    try:
        plan = await asyncio.to_thread(planner.run, build_context(v, now))
    except Exception:
        logger.exception("planner failed")
        await context.bot.send_message(chat_id=chat_id, text="Planning failed — check bot logs.")
        return
    if not plan.options:
        await context.bot.send_message(chat_id=chat_id, text=plan.situation)
        return
    text = planner.render(plan, now.date())
    path = f"plans/{now.date().isoformat()}.md"
    try:
        v.write_text(path, text, f"plan: {now.date().isoformat()}")
    except VaultError:
        logger.exception("saving plan failed")
    llm.flush(v)
    plan_key = now.strftime("%Y%m%d%H%M")
    context.bot_data.setdefault(_PLANS_KEY, {})[plan_key] = {"plan": plan, "path": path}
    chat_flow.remember(context, "The bot sent a travel plan", text)
    parts = chunks(text)
    for chunk in parts[:-1]:
        await context.bot.send_message(chat_id=chat_id, text=chunk)
    await context.bot.send_message(
        chat_id=chat_id, text=parts[-1], reply_markup=_keyboard(plan_key, plan)
    )


async def plan_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_authorized(update):
        return
    message = update.message
    assert message is not None
    await make_plan(context, message.chat_id)


async def handle_plan_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_authorized(update):
        return
    query = update.callback_query
    assert query is not None and query.data is not None
    await query.answer()
    _, action, plan_key, idx = query.data.split(":", 3)
    stored: dict[str, Any] | None = context.bot_data.get(_PLANS_KEY, {}).get(plan_key)
    if stored is None:
        await query.edit_message_reply_markup(reply_markup=None)
        await context.bot.send_message(
            chat_id=allowed_user_id(), text="That plan expired (the bot restarted) — /plan again."
        )
        return
    if action == "hold":
        await query.edit_message_reply_markup(reply_markup=None)
        await context.bot.send_message(
            chat_id=allowed_user_id(), text="Held. Tell me what to change and I'll rework it."
        )
        return
    plan: planner.Plan = stored["plan"]
    option = plan.options[int(idx)]
    v = vault()
    data = v.read_itinerary()
    try:
        applied = planner.apply_option(data, option, datetime.now(tz()).date())
        v.write_itinerary(data, f"itinerary: plan {option.get('label')}")
        existing = v.read_text(stored["path"]) or ""
        v.write_text(
            stored["path"],
            existing + f"\n**Chosen: {chr(65 + int(idx))}. {option.get('label')}**\n",
            f"plan: chose {option.get('label')}",
        )
    except VaultError:
        logger.exception("applying plan failed")
        await context.bot.send_message(
            chat_id=allowed_user_id(), text="Saving failed — check bot logs."
        )
        return
    context.bot_data[_PLANS_KEY].pop(plan_key, None)
    await query.edit_message_reply_markup(reply_markup=None)
    await context.bot.send_message(
        chat_id=allowed_user_id(),
        text=f"Going with {option.get('label')}. Planned:\n" + "\n".join(f"- {a}" for a in applied),
    )


def latest_plan(v: Vault) -> str | None:
    docs = v.list_docs("plans")
    return v.read_text(docs[-1]) if docs else None
