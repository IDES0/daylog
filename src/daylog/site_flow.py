"""Site analytics in the brief: the Site block.

Does nothing unless POSTHOG_PERSONAL_API_KEY and POSTHOG_PROJECT_ID are
set. A PostHog failure is logged and skipped. The full report is
GET /api/site.
"""

from __future__ import annotations

import logging
from datetime import date

from daylog import analytics
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
