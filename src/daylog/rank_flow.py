"""Telegram side of rankings: tier buttons, then head-to-head comparisons.

After an entry is logged, each newly visited place of a rankable kind
(food, surf spot, stay, ...) that hasn't been ranked yet gets a "How was
it?" prompt. Picking a tier starts a binary insertion (rankings.py): one
"which was better?" question at a time until the slot is found, then
rankings.yaml is written once.
"""

from __future__ import annotations

import logging
import uuid
from typing import Any

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Message, Update
from telegram.ext import ContextTypes

from daylog import rankings
from daylog.places import PlaceIndex
from daylog.tg import chunks, is_authorized, vault
from daylog.vault import VaultError

logger = logging.getLogger(__name__)

RANKINGS_FILE = "rankings.yaml"
_PENDING_KEY = "pending_rankings"
# Don't turn one voice note into a questionnaire.
MAX_PROMPTS_PER_ENTRY = 2


def _name(index: PlaceIndex, place_id: str) -> str:
    node = index.get(place_id)
    return str(node.get("name")) if node else place_id


def rank_candidates(facts: dict[str, Any], index: PlaceIndex) -> list[tuple[str, str | None]]:
    """(place_id, kind) for every linked place in an entry's activities and meals."""
    out: list[tuple[str, str | None]] = []
    for field_name in ("meals", "activities"):
        for item in facts.get(field_name) or []:
            if not isinstance(item, dict):
                continue
            place_id = item.get("place")
            node = index.get(place_id)
            if node is not None:
                out.append((str(place_id), node.get("kind")))
    return out


def _pending(context: ContextTypes.DEFAULT_TYPE) -> dict[str, dict[str, Any]]:
    # bot_data, not user_data: prompts can come from background jobs with no
    # user context, and this is a single-user bot anyway.
    store: dict[str, dict[str, Any]] = context.bot_data.setdefault(_PENDING_KEY, {})
    return store


async def ask_to_rank(
    message: Message, context: ContextTypes.DEFAULT_TYPE, place_id: str, category: str, name: str
) -> None:
    await ask_to_rank_chat(context, message.chat_id, place_id, category, name)


async def ask_to_rank_chat(
    context: ContextTypes.DEFAULT_TYPE, chat_id: int, place_id: str, category: str, name: str
) -> None:
    token = uuid.uuid4().hex[:12]
    _pending(context)[token] = {"place_id": place_id, "category": category, "ins": None}
    keyboard = InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("👍 Liked it", callback_data=f"rank:tier:{token}:liked"),
                InlineKeyboardButton("😐 Fine", callback_data=f"rank:tier:{token}:fine"),
                InlineKeyboardButton("👎 Didn't", callback_data=f"rank:tier:{token}:disliked"),
            ],
            [InlineKeyboardButton("Skip", callback_data=f"rank:tier:{token}:skip")],
        ]
    )
    await context.bot.send_message(
        chat_id=chat_id, text=f"How was {name}? ({category})", reply_markup=keyboard
    )


async def prompt_after_entry(
    message: Message, context: ContextTypes.DEFAULT_TYPE, facts: dict[str, Any]
) -> None:
    """Ask about up to MAX_PROMPTS_PER_ENTRY unranked places from a logged entry."""
    v = vault()
    index = PlaceIndex(v.read_places())
    current = v.read_yaml(RANKINGS_FILE, {})
    todo = rankings.unranked(current, rank_candidates(facts, index))
    for place_id, category in todo[:MAX_PROMPTS_PER_ENTRY]:
        await ask_to_rank(message, context, place_id, category, _name(index, place_id))


def _compare_keyboard(token: str, new_name: str, other_name: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton(new_name[:40], callback_data=f"rank:cmp:{token}:better")],
            [InlineKeyboardButton(other_name[:40], callback_data=f"rank:cmp:{token}:worse")],
            [InlineKeyboardButton("Too close to call", callback_data=f"rank:cmp:{token}:tie")],
        ]
    )


async def handle_rank_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_authorized(update):
        return
    query = update.callback_query
    assert query is not None and query.data is not None
    await query.answer()
    _, step, token, choice = query.data.split(":", 3)

    pending = _pending(context)
    state = pending.get(token)
    if state is None:
        await query.edit_message_text("This ranking has expired or was already handled.")
        return

    v = vault()
    index = PlaceIndex(v.read_places())
    current = v.read_yaml(RANKINGS_FILE, {})
    new_name = _name(index, state["place_id"])

    if step == "tier":
        if choice == "skip":
            pending.pop(token, None)
            await query.edit_message_text(f"Skipped ranking {new_name}.")
            return
        state["ins"] = rankings.start(current, state["category"], state["place_id"], choice)
    else:
        ins: rankings.Insertion = state["ins"]
        if choice == "tie":
            state["ins"] = rankings.settle(ins)
        else:
            state["ins"] = rankings.answer(ins, new_is_better=choice == "better")

    opponent = rankings.next_opponent(current, state["ins"])
    if opponent is not None:
        await query.edit_message_text(
            "Which was better?",
            reply_markup=_compare_keyboard(token, new_name, _name(index, opponent)),
        )
        return

    pending.pop(token, None)
    rankings.finish(current, state["ins"])
    try:
        v.write_yaml(RANKINGS_FILE, current, f"rankings: {state['category']} +{state['place_id']}")
    except VaultError:
        logger.exception("rankings commit failed for %s", state["place_id"])
        await query.edit_message_text(f"Ranked {new_name}, but saving failed — check bot logs.")
        return
    line = rankings.describe(current, state["category"], state["place_id"])
    await query.edit_message_text(f"Ranked {new_name}: {line}")


async def rank_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/rank <place> — rank (or re-rank) any known place."""
    if not is_authorized(update):
        return
    message = update.message
    assert message is not None
    query = " ".join(context.args or []).strip()
    if not query:
        await message.reply_text("Usage: /rank <place>, e.g. /rank Artisan")
        return
    index = PlaceIndex(vault().read_places())
    matches = index.resolve(query) or [
        i for i, n in index.by_id.items() if query.lower() in str(n.get("name", "")).lower()
    ]
    if not matches:
        await message.reply_text(f"No place matching '{query}' yet.")
        return
    node = index.by_id[matches[0]]
    category = rankings.category_for(node.get("kind"))
    if category is None:
        await message.reply_text(f"{node.get('name')} is a {node.get('kind')} — not ranked.")
        return
    await ask_to_rank(message, context, node["id"], category, str(node.get("name")))


def format_rankings(current: Any, index: PlaceIndex, category: str | None) -> str:
    categories = [category] if category else list(rankings.CATEGORIES)
    blocks = []
    for cat in categories:
        order = rankings.ranked(current, cat)
        if not order:
            continue
        lines = [f"{cat.upper()}"]
        for i, (place_id, _tier) in enumerate(order, start=1):
            score = rankings.score(current, cat, place_id)
            region = index.region_of(place_id)
            where = f" · {region.get('name')}" if region else ""
            lines.append(f"{i}. {_name(index, place_id)} — {score:.1f}{where}")
        blocks.append("\n".join(lines))
    return "\n\n".join(blocks) if blocks else "Nothing ranked yet."


async def rankings_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/rankings [food|surf|dive|stay|outdoors|nightlife]"""
    if not is_authorized(update):
        return
    message = update.message
    assert message is not None
    arg = context.args[0] if context.args else None
    category = arg.lower() if arg and arg.lower() in rankings.CATEGORIES else None
    v = vault()
    text = format_rankings(v.read_yaml(RANKINGS_FILE, {}), PlaceIndex(v.read_places()), category)
    for chunk in chunks(text):
        await message.reply_text(chunk)
