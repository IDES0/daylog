"""Telegram side of the site analytics: the brief's Site block and /site.

Both do nothing unless POSTHOG_PERSONAL_API_KEY and POSTHOG_PROJECT_ID
are set. A PostHog failure is logged and skipped in the brief, and
reported plainly by /site.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import date

from telegram import Update
from telegram.ext import ContextTypes

from daylog import analytics
from daylog.tg import chunks, is_authorized
from daylog.vault import Vault

logger = logging.getLogger(__name__)

DEFAULT_REPORT_DAYS = 7


def brief_section(v: Vault, today: date) -> str | None:
    """The brief's Site block; also records the day's summary in the vault."""
    if not analytics.configured():
        return None
    try:
        summary, company = analytics.fetch_brief()
    except analytics.AnalyticsError as e:
        logger.warning("site summary failed: %s", e)
        return None
    state = v.read_yaml(analytics.FILE, {}) or {}
    v.write_yaml(
        analytics.FILE,
        analytics.record_day(state, today.isoformat(), summary, company),
        f"site: {today.isoformat()}",
    )
    return analytics.format_brief(summary, company)


async def site_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/site [days] — pages, sources, countries, engagement and company refs."""
    if not is_authorized(update):
        return
    message = update.message
    assert message is not None
    if not analytics.configured():
        await message.reply_text("Site analytics aren't set up (no PostHog key).")
        return
    args = context.args or []
    try:
        days = int(args[0]) if args else DEFAULT_REPORT_DAYS
    except ValueError:
        await message.reply_text("Usage: /site or /site 30")
        return
    try:
        report = await asyncio.to_thread(analytics.fetch_report, days)
    except analytics.AnalyticsError as e:
        logger.warning("site report failed: %s", e)
        await message.reply_text("Couldn't reach PostHog — check bot logs.")
        return
    for chunk in chunks(report):
        await message.reply_text(chunk, disable_web_page_preview=True)
