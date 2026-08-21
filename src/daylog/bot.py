"""Telegram handlers and entrypoint.

Every handler checks the sender against TELEGRAM_ALLOWED_USER_ID before
doing anything else — this is the only auth layer, so it must run first.
"""

from __future__ import annotations

import logging
import os
import tempfile
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from dotenv import load_dotenv
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Message, Update
from telegram.ext import (
    Application,
    ApplicationBuilder,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

from daylog import extract, goals, itinerary, transcribe
from daylog.dateparse import parse_date_phrase
from daylog.vault import Vault, VaultError

logger = logging.getLogger(__name__)

_PENDING_DATE_KEY = "pending_entry_date"
_PENDING_SLIP_KEY = "pending_goal_slips"
_PENDING_ITIN_KEY = "pending_itinerary_changes"


def _allowed_user_id() -> int:
    return int(os.environ["TELEGRAM_ALLOWED_USER_ID"])


def _vault() -> Vault:
    return Vault(Path(os.environ.get("VAULT_PATH", "../daylog-vault")))


def _tz() -> ZoneInfo:
    return ZoneInfo(os.environ.get("TZ", "UTC"))


def _is_authorized(update: Update) -> bool:
    user = update.effective_user
    if user is None or user.id != _allowed_user_id():
        logger.warning("rejected update from unauthorized user id=%s", user.id if user else None)
        return False
    return True


def _active_goals_summary(goals_data: Any) -> list[dict[str, Any]]:
    return [
        {
            "id": g["id"],
            "title": g.get("title", g["id"]),
            "type": g.get("type", "soft"),
            "metric": g.get("metric"),
        }
        for g in goals_data
        if g.get("status", "active") == "active"
    ]


def _goals_commit_message(
    applied_progress: list[goals.AppliedProgress], applied_slips: list[goals.AppliedSlip]
) -> str:
    parts = [f"{p.goal_id} +{p.delta:g}" for p in applied_progress]
    parts += [f"slip {s.goal_id}" for s in applied_slips]
    return "goals: " + ", ".join(parts)


def _goals_reply_note(
    applied_progress: list[goals.AppliedProgress], applied_slips: list[goals.AppliedSlip]
) -> str:
    lines = [f"{p.title}: +{p.delta:g} (total {p.new_progress:g})" for p in applied_progress]
    lines += [f"{s.title}: moved to {s.new_date}" for s in applied_slips]
    return "\n".join(lines)


def _active_itinerary_summary(itinerary_data: Any) -> list[dict[str, Any]]:
    return [
        {
            "id": e["id"],
            "place": e.get("place", e["id"]),
            "type": e.get("type", "soft"),
            "status": e.get("status", "candidate"),
            "date": itinerary.current_date(e),
        }
        for e in itinerary_data
        if e.get("status") not in ("done", "dropped")
    ]


def _itinerary_commit_message(applied: list[itinerary.AppliedChange]) -> str:
    return "itinerary: " + ", ".join(a.place for a in applied)


def _itinerary_reply_note(applied: list[itinerary.AppliedChange]) -> str:
    return "\n".join(a.summary for a in applied)


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _is_authorized(update):
        return
    message = update.message
    assert message is not None
    await message.reply_text(
        "daylog is listening. Send a voice note or text to log your day.\n\n"
        'To log for a different day, send the date first (e.g. "yesterday", '
        '"2 days ago", "2026-08-20") — it applies to your next message only.'
    )


async def _ask_hard_slip_confirmation(
    message: Message, context: ContextTypes.DEFAULT_TYPE, slip: goals.PendingSlip
) -> None:
    assert context.user_data is not None
    confirm_id = uuid.uuid4().hex
    context.user_data.setdefault(_PENDING_SLIP_KEY, {})[confirm_id] = slip

    old = slip.old_date or "unset"
    reason = f" ({slip.reason})" if slip.reason else ""
    keyboard = InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("Confirm", callback_data=f"goalslip:confirm:{confirm_id}"),
                InlineKeyboardButton("Cancel", callback_data=f"goalslip:cancel:{confirm_id}"),
            ]
        ]
    )
    await message.reply_text(
        f"'{slip.title}' is a hard deadline — move it from {old} to {slip.new_date}?{reason}",
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

    vault = _vault()
    goals_data = vault.read_goals()
    goals.apply_confirmed_slip(goals_data, slip, on=datetime.now(_tz()).date())
    try:
        vault.write_goals(goals_data, f"goals: slip {slip.goal_id}")
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

    old = change.old_date or "unset"
    new_note = " (new entry)" if change.is_new else ""
    reason = f" ({change.reason})" if change.reason else ""
    keyboard = InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("Confirm", callback_data=f"itin:confirm:{confirm_id}"),
                InlineKeyboardButton("Cancel", callback_data=f"itin:cancel:{confirm_id}"),
            ]
        ]
    )
    await message.reply_text(
        f"'{change.place}'{new_note} is a hard date — set it from {old} to "
        f"{change.new_date}?{reason}",
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

    vault = _vault()
    itinerary_data = vault.read_itinerary()
    itinerary.apply_confirmed_change(itinerary_data, change, on=datetime.now(_tz()).date())
    try:
        vault.write_itinerary(itinerary_data, f"itinerary: {change.place}")
    except VaultError:
        logger.exception("itinerary commit failed confirming change for %s", change.id)
        await query.edit_message_text(
            f"Confirmed, but the git commit failed for '{change.place}' — check bot logs."
        )
        return

    await query.edit_message_text(f"Confirmed — '{change.place}' set to {change.new_date}.")


async def _log_entry(transcript: str, message: Message, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Shared pipeline: transcript text -> extract -> apply goals -> vault write -> reply.

    Used by both the voice and text handlers once each has produced a
    transcript string — voice via faster-whisper, text directly from the
    Telegram message.
    """
    try:
        await message.reply_text("Extracting...")

        pending_date = context.user_data.pop(_PENDING_DATE_KEY, None) if context.user_data else None
        now = datetime.now(_tz())
        entry_time = datetime.combine(pending_date, now.time()) if pending_date else now

        vault = _vault()
        goals_data = vault.read_goals()
        itinerary_data = vault.read_itinerary()
        facts = extract.extract(
            transcript,
            _active_goals_summary(goals_data),
            _active_itinerary_summary(itinerary_data),
            now.date(),
        )
        summary = facts.pop("summary", "")

        # goal_progress stays in facts — it's part of the journal frontmatter
        # schema too (SPEC §5.1) — but goal_slips and itinerary_changes are
        # goals.yaml/itinerary.yaml bookkeeping, not facts about the day, so
        # neither belongs in the journal file.
        goal_progress = facts.get("goal_progress", [])
        goal_slips = facts.pop("goal_slips", [])
        itinerary_changes = facts.pop("itinerary_changes", [])

        goals_note = ""
        if goal_progress or goal_slips:
            applied_progress = goals.apply_progress(goals_data, goal_progress)
            applied_slips, pending_slips = goals.apply_slips(
                goals_data, goal_slips, on=entry_time.date()
            )

            if applied_progress or applied_slips:
                try:
                    vault.write_goals(
                        goals_data, _goals_commit_message(applied_progress, applied_slips)
                    )
                    goals_note = "\n\n" + _goals_reply_note(applied_progress, applied_slips)
                except VaultError:
                    logger.exception("goals commit failed")
                    goals_note = "\n\n(goal update didn't save — check bot logs)"

            for slip in pending_slips:
                await _ask_hard_slip_confirmation(message, context, slip)

        itinerary_note = ""
        if itinerary_changes:
            applied_changes, pending_changes = itinerary.apply_itinerary_changes(
                itinerary_data, itinerary_changes, on=entry_time.date()
            )

            if applied_changes:
                try:
                    vault.write_itinerary(
                        itinerary_data, _itinerary_commit_message(applied_changes)
                    )
                    itinerary_note = "\n\n" + _itinerary_reply_note(applied_changes)
                except VaultError:
                    logger.exception("itinerary commit failed")
                    itinerary_note = "\n\n(itinerary update didn't save — check bot logs)"

            for change in pending_changes:
                await _ask_hard_itinerary_confirmation(message, context, change)

        try:
            vault.write_journal_entry(entry_time, facts, transcript, summary)
        except VaultError:
            # write_journal_entry writes the file to disk before it commits,
            # so a VaultError here means the entry is sitting on disk,
            # untracked — not lost, just not committed. Say so precisely,
            # since "nothing was saved" would be wrong and send the user
            # looking for a bug that isn't there.
            logger.exception("vault commit failed for %s", entry_time.date())
            await message.reply_text(
                f"Transcribed {entry_time.date().isoformat()} and wrote it to the vault, but "
                "the git commit failed — it's on disk, just not committed yet. Check the "
                "bot's logs (likely a git config issue) and it'll get picked up next time "
                "you log."
            )
            return

        await message.reply_text(
            f"Logged {entry_time.date().isoformat()}:\n\n{summary}{goals_note}{itinerary_note}"
        )
    except Exception:
        logger.exception("failed to process entry")
        await message.reply_text(
            "Something went wrong logging that. Nothing was saved — try again?"
        )


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


async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _is_authorized(update):
        return
    message = update.message
    assert message is not None and message.text is not None
    text = message.text

    today = datetime.now(_tz()).date()
    override_date = parse_date_phrase(text, today=today)
    if override_date is not None:
        assert context.user_data is not None
        context.user_data[_PENDING_DATE_KEY] = override_date
        await message.reply_text(
            f"Got it — your next message will be logged as {override_date.isoformat()}."
        )
        return

    await _log_entry(text, message, context)


def build_application() -> Application:  # type: ignore[type-arg]
    token = os.environ["TELEGRAM_BOT_TOKEN"]
    application = ApplicationBuilder().token(token).build()
    application.add_handler(CommandHandler("start", start))
    application.add_handler(MessageHandler(filters.VOICE, handle_voice))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))
    application.add_handler(CallbackQueryHandler(handle_goal_slip_callback, pattern=r"^goalslip:"))
    application.add_handler(CallbackQueryHandler(handle_itinerary_callback, pattern=r"^itin:"))
    return application


def main() -> None:
    load_dotenv()
    logging.basicConfig(level=logging.INFO)
    # httpx (and the libraries built on it) log every request at INFO,
    # which drowns daylog's own logs under the constant getUpdates polling.
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
    application = build_application()
    application.run_polling()


if __name__ == "__main__":
    main()
