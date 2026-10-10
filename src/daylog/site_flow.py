"""Telegram side of the site analytics: the company-ref poll and the brief section.

Both are no-ops unless POSTHOG_PERSONAL_API_KEY is set. A PostHog failure
is logged and skipped, never surfaced as a broken brief.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import UTC, date, datetime

from telegram.ext import ContextTypes

from daylog import analytics
from daylog.tg import allowed_user_id, vault
from daylog.vault import Vault

logger = logging.getLogger(__name__)


async def poll_job(context: ContextTypes.DEFAULT_TYPE) -> None:
    """Every ~15 min: alert once per person+ref for each new company-ref visitor."""
    if not analytics.configured():
        return
    v = vault()
    state = v.read_yaml(analytics.FILE, {}) or {}
    now = datetime.now(UTC)
    try:
        rows = await asyncio.to_thread(
            analytics.query, analytics.visits_hogql(analytics.poll_since(state, now))
        )
    except analytics.AnalyticsError as e:
        logger.warning("site poll failed: %s", e)
        return
    fresh, new_state = analytics.new_visits(state, rows, now)
    if not fresh:
        return
    for visit in fresh:
        await context.bot.send_message(
            chat_id=allowed_user_id(),
            text=analytics.format_alert(visit),
            disable_web_page_preview=True,
        )
    refs = ", ".join(sorted({visit.ref for visit in fresh}))
    v.write_yaml(analytics.FILE, new_state, f"site: visit from {refs}")


def brief_section(v: Vault, today: date) -> str | None:
    """The brief's "Site" block, recording the day's summary in the vault."""
    if not analytics.configured():
        return None
    try:
        summary = analytics.fetch_summary()
    except analytics.AnalyticsError as e:
        logger.warning("site summary failed: %s", e)
        return None
    state = v.read_yaml(analytics.FILE, {}) or {}
    v.write_yaml(
        analytics.FILE,
        analytics.record_day(state, today.isoformat(), summary),
        f"site: {today.isoformat()}",
    )
    return analytics.format_section(summary)
