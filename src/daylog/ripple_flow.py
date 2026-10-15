"""Telegram side of proposals: a decision's changes on one card.

The morning research routine reads new `decisions` in the journal, traces
each through the itinerary, goals and plan, and writes a proposal file
(see proposals.py). After the brief, the bot shows every pending proposal
as one card: apply every change (and the revised plan, if there is one),
go through them one by one as ordinary chat confirm cards, or skip. The
file records what was chosen, so a proposal is shown until it's answered.

`/ripple <decision>` does the same analysis on the spot with the bot's own
model, for when the morning is too long to wait.
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from datetime import datetime
from typing import Any

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Message, Update
from telegram.ext import ContextTypes

from daylog import chat_flow, edits, itinerary, llm, plan_flow, proposals, ripple
from daylog.tg import chunks, is_authorized, tz, vault
from daylog.vault import Vault, VaultError

logger = logging.getLogger(__name__)

_CARDS_KEY = "proposal_cards"
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


def _ids(v: Vault) -> tuple[set[str], set[str]]:
    return (
        {str(e["id"]) for e in v.read_itinerary() or []},
        {str(g["id"]) for g in v.read_goals() or []},
    )


def analyze(v: Vault, now: datetime, decision: str) -> str:
    """/ripple: run the analysis, save it and a proposal file. Returns the proposal path."""
    result = ripple.run(build_context(v, now, decision))
    result.changes = ripple.valid_changes(result.changes, *_ids(v))
    stem = f"{now.date().isoformat()}-{ripple.slug(result.decision)}"
    analysis = f"research/decisions/{stem}.md"
    v.write_text(analysis, ripple.render(result, now.date()), f"decisions: {result.decision}")
    path = f"{proposals.FOLDER}/{stem}.yaml"
    data = proposals.build(result, now.date().isoformat(), analysis)
    v.write_yaml(path, data, f"proposals: {result.decision}")
    return path


def pending(v: Vault) -> list[str]:
    return [
        p
        for p in v.list_docs(proposals.FOLDER, "*.yaml")
        if proposals.is_pending(v.read_yaml(p, None))
    ]


def _keyboard(token: str, n: int, has_plan: bool) -> InlineKeyboardMarkup:
    label = f"Apply all {n}" + (" + new plan" if has_plan else "") if n else "Use the new plan"
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton(label, callback_data=f"ripple:all:{token}")],
            [
                InlineKeyboardButton("One by one", callback_data=f"ripple:each:{token}"),
                InlineKeyboardButton("Skip", callback_data=f"ripple:no:{token}"),
            ],
        ]
    )


async def send_card(context: ContextTypes.DEFAULT_TYPE, chat_id: int, v: Vault, path: str) -> None:
    data = v.read_yaml(path, None)
    if not proposals.is_pending(data):
        return
    changes = proposals.changes_for(data, *_ids(v))
    if not changes and not data.get("plan_draft"):
        _mark(v, path, "reviewed", ["nothing left to change"])
        return
    token = uuid.uuid4().hex[:10]
    context.bot_data.setdefault(_CARDS_KEY, {})[token] = path
    for chunk in chunks(proposals.card_text(data, changes)):
        await context.bot.send_message(chat_id=chat_id, text=chunk)
    await context.bot.send_message(
        chat_id=chat_id,
        text="What should I do with these?",
        reply_markup=_keyboard(token, len(changes), bool(data.get("plan_draft"))),
    )


async def send_pending(context: ContextTypes.DEFAULT_TYPE, chat_id: int) -> None:
    """After the brief: one card per proposal still waiting for an answer."""
    v = vault()
    for path in pending(v):
        try:
            await send_card(context, chat_id, v, path)
        except Exception:
            logger.exception("proposal card failed for %s", path)


def _mark(v: Vault, path: str, status: str, results: list[str]) -> None:
    data = v.read_yaml(path, {}) or {}
    data["status"] = status
    data["answered_on"] = datetime.now(tz()).date().isoformat()
    if results:
        data["results"] = results
    v.write_yaml(path, data, f"proposals: {status} {path.rsplit('/', 1)[-1]}")


def adopt_plan(v: Vault, data: dict[str, Any], today: str) -> str | None:
    """Make the proposal's draft plan the current one (the newest file in plans/)."""
    draft = data.get("plan_draft")
    text = v.read_text(str(draft)) if draft else None
    if not text:
        return None
    path = f"plans/{today}-plan.md"
    v.write_text(path, text, f"plans: adopted {draft}")
    return path


def apply_all(v: Vault, changes: list[dict[str, Any]], today: Any) -> tuple[list[str], list[str]]:
    done, failed = [], []
    for change in changes:
        try:
            done.append(chat_flow.apply_edit(v, change, today) or change["summary"])
        except (edits.EditError, ValueError) as exc:
            failed.append(f"{change['summary']}: {exc}")
        except VaultError:
            logger.exception("applying a proposed change failed")
            failed.append(f"{change['summary']}: saving failed")
    return done, failed


def _apply_proposal(v: Vault, path: str) -> str:
    data = v.read_yaml(path, {}) or {}
    today = datetime.now(tz()).date()
    done, failed = apply_all(v, proposals.changes_for(data, *_ids(v)), today)
    plan = adopt_plan(v, data, today.isoformat())
    if plan:
        done.append(f"current plan is now {plan}")
    _mark(v, path, "applied", done + [f"failed: {f}" for f in failed])
    lines = [f"Applied {len(done)}:", *[f"• {d}" for d in done]]
    if failed:
        lines += [f"Didn't apply {len(failed)}:", *[f"• {f}" for f in failed]]
    return "\n".join(lines)


_REPLAN = InlineKeyboardMarkup(
    [[InlineKeyboardButton("🗺 Make a fresh plan", callback_data="ripple:plan:-")]]
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
    path = context.bot_data.get(_CARDS_KEY, {}).pop(token, None)
    if path is None:
        await query.edit_message_text("This card expired (the bot restarted); it'll come back.")
        return
    v = vault()
    if action == "no":
        await asyncio.to_thread(_mark, v, path, "skipped", [])
        await query.edit_message_text("Left everything as is.")
        return
    if action == "each":
        data = v.read_yaml(path, {}) or {}
        await query.edit_message_text("One at a time:")
        if isinstance(query.message, Message):
            for change in proposals.changes_for(data, *_ids(v)):
                await chat_flow.send_proposal(query.message, context, change)
        if data.get("plan_draft") and chat_id is not None:
            plan_token = uuid.uuid4().hex[:10]
            context.bot_data.setdefault(_CARDS_KEY, {})[plan_token] = path
            await context.bot.send_message(
                chat_id=chat_id,
                text="And the revised plan?",
                reply_markup=InlineKeyboardMarkup(
                    [[InlineKeyboardButton("Use it", callback_data=f"ripple:adopt:{plan_token}")]]
                ),
            )
        await asyncio.to_thread(_mark, v, path, "reviewed", [])
        return
    if action == "adopt":
        data = v.read_yaml(path, {}) or {}
        plan = await asyncio.to_thread(adopt_plan, v, data, datetime.now(tz()).date().isoformat())
        await query.edit_message_text(f"Current plan is now {plan}." if plan else "No draft found.")
        return
    try:
        outcome = await asyncio.to_thread(_apply_proposal, v, path)
    except VaultError:
        logger.exception("applying proposal failed")
        await query.edit_message_text("Saving failed — check bot logs.")
        return
    await query.edit_message_text(outcome, reply_markup=_REPLAN)


async def run_for(context: ContextTypes.DEFAULT_TYPE, chat_id: int, decision: str) -> None:
    v = vault()
    if not llm.can_spend(v, RIPPLE_ESTIMATE_USD):
        await context.bot.send_message(
            chat_id=chat_id,
            text="The monthly budget is spent, so I can't trace that now. "
            "The morning research run will pick it up from your journal.",
        )
        return
    await context.bot.send_message(
        chat_id=chat_id, text=f"⚡ Tracing what this changes: {decision}"
    )
    try:
        path = await asyncio.to_thread(analyze, v, datetime.now(tz()), decision)
    except Exception:
        logger.exception("ripple failed")
        await context.bot.send_message(
            chat_id=chat_id, text="Couldn't trace that decision — check bot logs."
        )
        return
    finally:
        llm.flush(v)
    data = v.read_yaml(path, {}) or {}
    analysis = v.read_text(str(data.get("analysis", ""))) or ""
    for chunk in chunks(analysis.replace("**", "")):
        await context.bot.send_message(chat_id=chat_id, text=chunk)
    chat_flow.remember(context, "The bot traced a decision", analysis)
    await send_card(context, chat_id, v, path)


async def ripple_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/ripple <decision> — trace a decision now instead of waiting for the morning run."""
    if not is_authorized(update):
        return
    message = update.message
    assert message is not None
    decision = " ".join(context.args or []).strip()
    if not decision:
        await message.reply_text("Usage: /ripple I accepted the job in Austin from Oct 20")
        return
    context.application.create_task(run_for(context, message.chat_id, decision))
