"""Telegram handlers and entrypoint.

Every handler checks the sender against TELEGRAM_ALLOWED_USER_ID before
doing anything else — this is the only auth layer, so it must run first.
"""

from __future__ import annotations

import asyncio
import logging
import os
import tempfile
import uuid
from collections.abc import Callable, Coroutine
from datetime import date, datetime, timedelta
from datetime import time as dt_time
from pathlib import Path
from typing import Any

from dotenv import load_dotenv
from telegram import (
    BotCommand,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
    Update,
)
from telegram.ext import (
    Application,
    ApplicationBuilder,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

from daylog import (
    calendar_server,
    chat_flow,
    daily,
    discipline_flow,
    goals,
    itinerary,
    journal_service,
    llm,
    plan_flow,
    rank_flow,
    research_flow,
    ripple_flow,
    services,
    site_flow,
    transcribe,
    wishlist_flow,
)
from daylog.dateparse import parse_date_phrase
from daylog.journal_service import PendingCorrection, ResolvedOtherDayNote
from daylog.journal_service import resolve_corrections as _resolve_corrections
from daylog.journal_service import resolve_other_day_notes as _resolve_other_day_notes
from daylog.places import PlaceIndex
from daylog.services import conditions_reports
from daylog.services import fetch_marine_forecast as _fetch_marine_forecast
from daylog.services import fetch_wind_forecast as _fetch_wind_forecast
from daylog.services import format_status as _format_status
from daylog.services import upcoming_items as _upcoming_items
from daylog.tg import allowed_user_id as _allowed_user_id
from daylog.tg import chunks as _chunks
from daylog.tg import is_authorized as _is_authorized
from daylog.tg import tz as _tz
from daylog.tg import vault as _vault
from daylog.vault import CorrectionConflictError, VaultError

logger = logging.getLogger(__name__)

# Moved to journal_service / services (shared with the web app); the old
# names stay importable from here.
__all__ = [
    "PendingCorrection",
    "ResolvedOtherDayNote",
    "_fetch_marine_forecast",
    "_fetch_wind_forecast",
    "_format_status",
    "_resolve_corrections",
    "_resolve_other_day_notes",
    "_upcoming_items",
    "conditions_reports",
]

_PENDING_DATE_KEY = "pending_entry_date"
_PENDING_SLIP_KEY = "pending_goal_slips"
_PENDING_ITIN_KEY = "pending_itinerary_changes"
_PENDING_CORRECTION_KEY = "pending_corrections"


async def status(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _is_authorized(update):
        return
    message = update.message
    assert message is not None
    vault = _vault()
    await message.reply_text(_format_status(vault.read_goals(), vault.read_itinerary()))


async def upcoming(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _is_authorized(update):
        return
    message = update.message
    assert message is not None
    vault = _vault()
    today = datetime.now(_tz()).date()
    lines = _upcoming_items(vault.read_goals(), vault.read_itinerary(), today)
    await message.reply_text("\n".join(lines) if lines else "Nothing dated yet.")


async def trail_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _is_authorized(update):
        return
    message = update.message
    assert message is not None
    vault = _vault()
    today = datetime.now(_tz()).date()
    text = services.trail_text(vault, list(context.args or []), today)
    for chunk in _chunks(text):
        await message.reply_text(chunk)


async def place_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _is_authorized(update):
        return
    message = update.message
    assert message is not None
    query = " ".join(context.args or []).strip()
    if not query:
        await message.reply_text("Usage: /place <name>, e.g. /place Lakey Peak")
        return
    vault = _vault()
    index = PlaceIndex(vault.read_places())
    matches = services.find_places(index, query)
    if not matches:
        await message.reply_text(f"No place matching '{query}' yet.")
        return
    node = index.by_id[matches[0]]
    card = services.place_card_text(vault, index, node, datetime.now(_tz()).date())
    if len(matches) > 1:
        others = ", ".join(index.path_name(m) for m in matches[1:5])
        card += f"\n\nAlso matching: {others}"
    for chunk in _chunks(card):
        await message.reply_text(chunk)


def _brief_hour() -> int:
    return int(os.environ.get("BRIEF_HOUR", "7"))


async def surf_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/surf — rated conditions for every profiled spot around you (no API cost)."""
    if not _is_authorized(update):
        return
    message = update.message
    assert message is not None
    for chunk in _chunks(services.surf_text(_vault())):
        await message.reply_text(chunk)


async def fly_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/fly — paragliding flyability for launch sites around you (no API cost)."""
    if not _is_authorized(update):
        return
    message = update.message
    assert message is not None
    for chunk in _chunks(services.fly_text(_vault())):
        await message.reply_text(chunk)


async def _send_brief(context: ContextTypes.DEFAULT_TYPE, chat_id: int) -> None:
    bot = context.bot
    await bot.send_message(chat_id=chat_id, text="Building your brief...")
    try:
        vault = _vault()
        today = datetime.now(_tz()).date()
        text = await asyncio.to_thread(services.build_brief, vault, today)
    except Exception:
        logger.exception("failed to generate brief")
        await bot.send_message(
            chat_id=chat_id, text="Something went wrong building your brief — check bot logs."
        )
        return

    for chunk in _chunks(text):
        await bot.send_message(chat_id=chat_id, text=chunk)
    chat_flow.remember(context, "The bot sent the morning brief", text)
    llm.flush(vault)
    # Decisions the morning research traced (and anything else it proposed),
    # one card each, until they're answered.
    await ripple_flow.send_pending(context, chat_id)


async def brief_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _is_authorized(update):
        return
    message = update.message
    assert message is not None
    await _send_brief(context, message.chat_id)


async def send_scheduled_brief(context: ContextTypes.DEFAULT_TYPE) -> None:
    await _send_brief(context, _allowed_user_id())


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _is_authorized(update):
        return
    message = update.message
    assert message is not None
    await message.reply_text(
        "daylog is listening.\n\n"
        "🎙 A voice note logs today (before 4am it still counts as yesterday).\n"
        "📝 Log: pick a day, then your next voice note or message logs to it.\n"
        "🌊 Surf · 🪂 Fly · ☀️ Brief · 🗺 Plan run those right away; ⋯ More has the rest.\n"
        "💬 Anything else you type goes to the assistant — ask about your trips, "
        "plans, places, rankings, or where to go next.\n\n"
        'Corrections work by voice too ("actually I only surfed 1 hour") — I\'ll ask '
        "before removing anything.",
        reply_markup=chat_flow.MENU,
    )


def _format_short_date(d: date) -> str:
    return f"{d.strftime('%b')} {d.day}"


def _backdate_options(today: date, days: int = 7) -> list[tuple[date, str]]:
    """(date, label) pairs for the /backdate picker, today first then going back."""
    options = []
    for offset in range(days):
        d = today - timedelta(days=offset)
        if offset == 0:
            name = "Today"
        elif offset == 1:
            name = "Yesterday"
        else:
            name = d.strftime("%A")
        options.append((d, f"{name} ({_format_short_date(d)})"))
    return options


async def backdate_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _is_authorized(update):
        return
    message = update.message
    assert message is not None
    await _send_backdate_picker(message, daily.logical_day(datetime.now(_tz())))


async def _send_backdate_picker(message: Message, today: date) -> None:
    keyboard = InlineKeyboardMarkup(
        [
            [InlineKeyboardButton(label, callback_data=f"backdate:{d.isoformat()}")]
            for d, label in _backdate_options(today)
        ]
    )
    await message.reply_text("Log your next message under which day?", reply_markup=keyboard)


async def handle_backdate_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _is_authorized(update):
        return
    query = update.callback_query
    assert query is not None and query.data is not None
    await query.answer()

    _, iso_date = query.data.split(":", 1)
    picked = date.fromisoformat(iso_date)

    assert context.user_data is not None
    context.user_data[_PENDING_DATE_KEY] = picked

    await query.edit_message_text(
        f"Got it — your next message will be logged as {picked.isoformat()}."
    )


async def _ask_hard_slip_confirmation(
    message: Message, context: ContextTypes.DEFAULT_TYPE, slip: goals.PendingSlip
) -> None:
    assert context.user_data is not None
    confirm_id = uuid.uuid4().hex
    context.user_data.setdefault(_PENDING_SLIP_KEY, {})[confirm_id] = slip

    keyboard = InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("Confirm", callback_data=f"goalslip:confirm:{confirm_id}"),
                InlineKeyboardButton("Cancel", callback_data=f"goalslip:cancel:{confirm_id}"),
            ]
        ]
    )
    await message.reply_text(
        journal_service.slip_question(slip),
        reply_markup=keyboard,
    )


async def handle_goal_slip_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _is_authorized(update):
        return
    query = update.callback_query
    assert query is not None and query.data is not None
    await query.answer()

    _, action, confirm_id = query.data.split(":", 2)

    assert context.user_data is not None
    pending: dict[str, goals.PendingSlip] = context.user_data.get(_PENDING_SLIP_KEY, {})
    slip = pending.pop(confirm_id, None)
    if slip is None:
        await query.edit_message_text("This confirmation has expired or was already handled.")
        return

    if action == "cancel":
        await query.edit_message_text(f"Cancelled — '{slip.title}' deadline unchanged.")
        return

    try:
        journal_service.confirm_slip(_vault(), slip, datetime.now(_tz()).date())
    except VaultError:
        logger.exception("goals commit failed confirming slip for %s", slip.goal_id)
        await query.edit_message_text(
            f"Confirmed, but the git commit failed for '{slip.title}' — check bot logs."
        )
        return

    await query.edit_message_text(f"Confirmed — '{slip.title}' deadline moved to {slip.new_date}.")


async def _ask_hard_itinerary_confirmation(
    message: Message, context: ContextTypes.DEFAULT_TYPE, change: itinerary.PendingChange
) -> None:
    assert context.user_data is not None
    confirm_id = uuid.uuid4().hex
    context.user_data.setdefault(_PENDING_ITIN_KEY, {})[confirm_id] = change

    keyboard = InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("Confirm", callback_data=f"itin:confirm:{confirm_id}"),
                InlineKeyboardButton("Cancel", callback_data=f"itin:cancel:{confirm_id}"),
            ]
        ]
    )
    await message.reply_text(
        journal_service.change_question(change),
        reply_markup=keyboard,
    )


async def handle_itinerary_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _is_authorized(update):
        return
    query = update.callback_query
    assert query is not None and query.data is not None
    await query.answer()

    _, action, confirm_id = query.data.split(":", 2)

    assert context.user_data is not None
    pending: dict[str, itinerary.PendingChange] = context.user_data.get(_PENDING_ITIN_KEY, {})
    change = pending.pop(confirm_id, None)
    if change is None:
        await query.edit_message_text("This confirmation has expired or was already handled.")
        return

    if action == "cancel":
        await query.edit_message_text(f"Cancelled — '{change.place}' left unchanged.")
        return

    try:
        journal_service.confirm_change(_vault(), change, datetime.now(_tz()).date())
    except VaultError:
        logger.exception("itinerary commit failed confirming change for %s", change.id)
        await query.edit_message_text(
            f"Confirmed, but the git commit failed for '{change.place}' — check bot logs."
        )
        return

    await query.edit_message_text(f"Confirmed — '{change.place}' set to {change.new_date}.")


async def _ask_correction_confirmation(
    message: Message, context: ContextTypes.DEFAULT_TYPE, correction: PendingCorrection
) -> None:
    assert context.user_data is not None
    confirm_id = uuid.uuid4().hex
    context.user_data.setdefault(_PENDING_CORRECTION_KEY, {})[confirm_id] = correction

    keyboard = InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("Confirm", callback_data=f"correction:confirm:{confirm_id}"),
                InlineKeyboardButton("Cancel", callback_data=f"correction:cancel:{confirm_id}"),
            ]
        ]
    )
    await message.reply_text(
        journal_service.correction_question(correction),
        reply_markup=keyboard,
    )


async def handle_correction_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _is_authorized(update):
        return
    query = update.callback_query
    assert query is not None and query.data is not None
    await query.answer()

    _, action, confirm_id = query.data.split(":", 2)

    assert context.user_data is not None
    pending: dict[str, PendingCorrection] = context.user_data.get(_PENDING_CORRECTION_KEY, {})
    correction = pending.pop(confirm_id, None)
    if correction is None:
        await query.edit_message_text("This confirmation has expired or was already handled.")
        return

    if action == "cancel":
        await query.edit_message_text(f"Cancelled — '{correction.description}' left unchanged.")
        return

    try:
        journal_service.confirm_correction(_vault(), correction)
    except CorrectionConflictError:
        await query.edit_message_text(
            f"'{correction.description}' no longer matches today's entry — it may have "
            "already changed. Nothing was removed; say the correction again if it's still needed."
        )
        return
    except VaultError:
        logger.exception("correction commit failed for %s", correction.entry_date)
        await query.edit_message_text(
            f"Confirmed, but the git commit failed removing '{correction.description}' — "
            "check bot logs."
        )
        return

    await query.edit_message_text(
        f"Removed '{correction.description}' from {correction.entry_date.isoformat()}."
    )


async def _log_entry(transcript: str, message: Message, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Shared pipeline: transcript text -> journal_service.log -> replies and buttons.

    Used by both the voice and text handlers once each has produced a
    transcript string — voice via faster-whisper, text directly from the
    Telegram message. The web app calls the same journal_service.log.
    """
    try:
        await message.reply_text("Extracting...")

        pending_date = context.user_data.pop(_PENDING_DATE_KEY, None) if context.user_data else None
        now = datetime.now(_tz())
        # Before the day cutoff (4am by default) a note still belongs to the
        # day that's ending — see daily.logical_day.
        day = pending_date or daily.logical_day(now)

        vault = _vault()
        result = journal_service.log(vault, transcript, day, now, backdated=bool(pending_date))
        if result.location_changed:
            context.application.create_task(research_flow.arrive(context, message.chat_id))

        for slip in result.slips:
            await _ask_hard_slip_confirmation(message, context, slip)
        for change in result.changes:
            await _ask_hard_itinerary_confirmation(message, context, change)
        for correction in result.corrections:
            await _ask_correction_confirmation(message, context, correction)

        if not result.saved:
            # The entry is on disk, just not committed. Say so precisely,
            # since "nothing was saved" would be wrong and send the user
            # looking for a bug that isn't there.
            await message.reply_text(
                f"Transcribed {result.date.isoformat()} and wrote it to the vault, but "
                "the git commit failed — it's on disk, just not committed yet. Check the "
                "bot's logs (likely a git config issue) and it'll get picked up next time "
                "you log."
            )
            return

        notes = "".join(f"\n\n{block}" for block in result.blocks)
        await message.reply_text(
            f"Logged {result.date.isoformat()}:\n\n{result.summary}{notes}",
            reply_markup=chat_flow.MENU,
        )
        await discipline_flow.after_entry(
            message, context, result.graph, result.date, result.guesses, result.new_disciplines
        )
        try:
            await rank_flow.prompt_after_entry(message, context, result.facts)
        except Exception:
            logger.exception("ranking prompt failed")
        if result.place_mentions:
            context.application.create_task(
                research_flow.resolve_days(context, message.chat_id, [result.date])
            )
    except Exception:
        logger.exception("failed to process entry")
        await message.reply_text(
            "Something went wrong logging that, so nothing was saved. Here's what I heard, "
            "so it isn't lost — tap 📝 Log, pick the day, and paste it back, or just try again."
        )
        for chunk in _chunks(transcript):
            await message.reply_text(chunk)


async def handle_voice(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _is_authorized(update):
        return
    message = update.message
    assert message is not None and message.voice is not None

    try:
        with tempfile.TemporaryDirectory() as tmp_dir:
            ogg_path = Path(tmp_dir) / "voice.ogg"
            telegram_file = await message.voice.get_file()
            await telegram_file.download_to_drive(custom_path=ogg_path)

            await message.reply_text("Transcribing...")
            transcript = transcribe.transcribe(ogg_path)
    except Exception:
        logger.exception("failed to transcribe voice note")
        await message.reply_text(
            "Something went wrong transcribing that. Nothing was saved — try again?"
        )
        return

    if not transcript:
        await message.reply_text("Couldn't make out any speech in that voice note.")
        return

    await _log_entry(transcript, message, context)


_Command = Callable[[Update, ContextTypes.DEFAULT_TYPE], Coroutine[Any, Any, None]]


def _button_commands() -> dict[str, _Command]:
    """Menu button label -> the command it runs (the same handler as /command)."""
    return {
        chat_flow.BTN_SURF: surf_command,
        chat_flow.BTN_FLY: fly_command,
        chat_flow.BTN_BRIEF: brief_command,
        chat_flow.BTN_PLAN: plan_flow.plan_command,
        chat_flow.BTN_STATUS: status,
        chat_flow.BTN_DISCIPLINES: discipline_flow.disciplines_command,
        chat_flow.BTN_UPCOMING: upcoming,
        chat_flow.BTN_TRAIL: trail_command,
        chat_flow.BTN_RANKINGS: rank_flow.rankings_command,
        chat_flow.BTN_WISHLIST: wishlist_flow.wishlist_command,
        chat_flow.BTN_REVIEW: daily.review_command,
        chat_flow.BTN_UNDO: chat_flow.undo_command,
        chat_flow.BTN_USAGE: research_flow.usage_command,
    }


async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Menu buttons, then journal (when a day was picked), else the assistant."""
    if not _is_authorized(update):
        return
    message = update.message
    assert message is not None and message.text is not None
    text = message.text.strip()
    assert context.user_data is not None
    now = datetime.now(_tz())
    today = daily.logical_day(now)

    if text == chat_flow.BTN_LOG:
        await _send_backdate_picker(message, today)
        return
    if text == chat_flow.BTN_MORE:
        await message.reply_text("More:", reply_markup=chat_flow.MORE_MENU)
        return
    if text == chat_flow.BTN_BACK:
        await message.reply_text("Back to the main menu.", reply_markup=chat_flow.MENU)
        return
    command = _button_commands().get(text)
    if command is not None:
        await command(update, context)
        return

    if context.user_data.get(_PENDING_DATE_KEY) is not None:
        await _log_entry(text, message, context)
        return

    override_date = parse_date_phrase(text, today=today)
    if override_date is not None:
        context.user_data[_PENDING_DATE_KEY] = override_date
        await message.reply_text(
            f"Got it — your next message will be logged as {override_date.isoformat()}."
        )
        return

    await chat_flow.handle_chat(message, context, text)


# What Telegram's "/" list shows. Kept short on purpose — the reply keyboard
# covers the everyday actions, and every other command still works typed.
MENU_COMMANDS = (
    ("brief", "Get a daily brief now"),
    ("surf", "Rated surf forecast for spots around you"),
    ("plan", "Plan the next few weeks (2-3 options to pick from)"),
    ("status", "Show current goals and itinerary"),
    ("undo", "Revert one of the bot's recent changes"),
    ("start", "How to use daylog"),
)


async def _post_init(application: Application) -> None:  # type: ignore[type-arg]
    # Registers Telegram's native "/" command menu, so the available
    # commands are tappable instead of something to remember/type exactly.
    await application.bot.set_my_commands(
        [BotCommand(name, description) for name, description in MENU_COMMANDS]
    )


def build_application() -> Application:  # type: ignore[type-arg]
    token = os.environ["TELEGRAM_BOT_TOKEN"]
    application = ApplicationBuilder().token(token).post_init(_post_init).build()
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("status", status))
    application.add_handler(CommandHandler("upcoming", upcoming))
    application.add_handler(CommandHandler("brief", brief_command))
    application.add_handler(CommandHandler("backdate", backdate_command))
    application.add_handler(CommandHandler("trail", trail_command))
    application.add_handler(CommandHandler("place", place_command))
    application.add_handler(CommandHandler("rank", rank_flow.rank_command))
    application.add_handler(CommandHandler("reconcile", daily.reconcile_command))
    application.add_handler(CommandHandler("explore", research_flow.explore_command))
    application.add_handler(CommandHandler("research", research_flow.research_command))
    application.add_handler(CommandHandler("trip", research_flow.trip_command))
    application.add_handler(CommandHandler("backfill", research_flow.backfill_command))
    application.add_handler(CommandHandler("usage", research_flow.usage_command))
    application.add_handler(CommandHandler("surf", surf_command))
    application.add_handler(CommandHandler("fly", fly_command))
    application.add_handler(CommandHandler("site", site_flow.site_command))
    application.add_handler(CommandHandler("disciplines", discipline_flow.disciplines_command))
    application.add_handler(CommandHandler("ripple", ripple_flow.ripple_command))
    application.add_handler(CallbackQueryHandler(ripple_flow.handle_callback, pattern=r"^ripple:"))
    application.add_handler(CommandHandler("want", wishlist_flow.want_command))
    application.add_handler(CommandHandler("wishlist", wishlist_flow.wishlist_command))
    application.add_handler(
        CallbackQueryHandler(wishlist_flow.handle_want_callback, pattern=r"^want:")
    )
    application.add_handler(
        CallbackQueryHandler(chat_flow.handle_proposal_callback, pattern=r"^chatitin:")
    )
    application.add_handler(CommandHandler("plan", plan_flow.plan_command))
    application.add_handler(CommandHandler("review", daily.review_command))
    application.add_handler(CommandHandler("undo", chat_flow.undo_command))
    application.add_handler(CallbackQueryHandler(chat_flow.handle_undo_callback, pattern=r"^undo:"))
    application.add_handler(CallbackQueryHandler(plan_flow.handle_plan_callback, pattern=r"^plan:"))
    application.add_handler(
        CallbackQueryHandler(research_flow.handle_place_callback, pattern=r"^place:")
    )
    application.add_handler(CommandHandler("rankings", rank_flow.rankings_command))
    application.add_handler(CallbackQueryHandler(rank_flow.handle_rank_callback, pattern=r"^rank:"))
    application.add_handler(MessageHandler(filters.VOICE, handle_voice))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))
    application.add_handler(CallbackQueryHandler(handle_goal_slip_callback, pattern=r"^goalslip:"))
    application.add_handler(CallbackQueryHandler(handle_itinerary_callback, pattern=r"^itin:"))
    application.add_handler(
        CallbackQueryHandler(handle_correction_callback, pattern=r"^correction:")
    )
    application.add_handler(CallbackQueryHandler(handle_backdate_callback, pattern=r"^backdate:"))
    application.add_handler(
        CallbackQueryHandler(discipline_flow.handle_callback, pattern=r"^disc:")
    )

    assert application.job_queue is not None
    application.job_queue.run_daily(
        send_scheduled_brief, time=dt_time(hour=_brief_hour(), tzinfo=_tz())
    )
    application.job_queue.run_daily(
        daily.reconcile_job, time=dt_time(hour=daily.day_cutoff_hour(), minute=5, tzinfo=_tz())
    )
    # PTB counts days from Sunday = 0.
    application.job_queue.run_daily(
        daily.weekly_review_job,
        time=dt_time(hour=daily.review_hour(), tzinfo=_tz()),
        days=(0,),
    )
    return application


def main() -> None:
    load_dotenv()
    logging.basicConfig(level=logging.INFO)
    # httpx (and the libraries built on it) log every request at INFO,
    # which drowns daylog's own logs under the constant getUpdates polling.
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
    calendar_server.start()
    application = build_application()
    application.run_polling()


if __name__ == "__main__":
    main()
