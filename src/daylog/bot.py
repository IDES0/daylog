"""Telegram handlers and entrypoint.

Every handler checks the sender against TELEGRAM_ALLOWED_USER_ID before
doing anything else — this is the only auth layer, so it must run first.
"""

from __future__ import annotations

import logging
import os
import tempfile
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from dotenv import load_dotenv
from telegram import Message, Update
from telegram.ext import (
    Application,
    ApplicationBuilder,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

from daylog import extract, transcribe
from daylog.dateparse import parse_date_phrase
from daylog.vault import Vault, VaultError

logger = logging.getLogger(__name__)

_PENDING_DATE_KEY = "pending_entry_date"


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


async def _log_entry(transcript: str, message: Message, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Shared pipeline: transcript text -> extract -> vault write -> reply.

    Used by both the voice and text handlers once each has produced a
    transcript string — voice via faster-whisper, text directly from the
    Telegram message.
    """
    try:
        await message.reply_text("Extracting...")
        facts = extract.extract(transcript)
        summary = facts.pop("summary", "")

        pending_date = context.user_data.pop(_PENDING_DATE_KEY, None) if context.user_data else None
        if pending_date is not None:
            entry_time = datetime.combine(pending_date, datetime.now(_tz()).time())
        else:
            entry_time = datetime.now(_tz())

        vault = _vault()
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

        await message.reply_text(f"Logged {entry_time.date().isoformat()}:\n\n{summary}")
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
