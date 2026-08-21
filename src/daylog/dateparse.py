"""Deterministic parsing for the small vocabulary of date-override phrases.

Deliberately not LLM-based: this is a control-flow signal ("log the next
entry under this date"), not fact extraction from a rambling transcript, so
it should be exact and predictable rather than inferred. A message only
counts as a date override if the *entire* stripped text matches one of these
patterns — a narrative message that happens to mention "yesterday" in
passing falls through and is treated as journal content instead.
"""

from __future__ import annotations

import re
from datetime import date, datetime, timedelta

_DAYS_AGO_RE = re.compile(r"^(\d+)\s+days?\s+ago$")

_MONTH_DAY_YEAR_FORMATS = ("%B %d %Y", "%B %d, %Y", "%b %d %Y", "%b %d, %Y")
# Formats with no year in the input: the year is appended explicitly before
# parsing (rather than relying on strptime's implicit default-year behavior,
# which is deprecated as of Python 3.13 and changes in 3.15).
_MONTH_DAY_FORMATS = ("%B %d %Y", "%b %d %Y")


def parse_date_phrase(text: str, today: date) -> date | None:
    """Return the date `text` refers to, or None if it isn't a date phrase."""
    normalized = text.strip().lower()
    if not normalized:
        return None

    if normalized == "today":
        return today
    if normalized == "yesterday":
        return today - timedelta(days=1)

    days_ago = _DAYS_AGO_RE.match(normalized)
    if days_ago:
        return today - timedelta(days=int(days_ago.group(1)))

    try:
        return date.fromisoformat(text.strip())
    except ValueError:
        pass

    title_cased = text.strip().title()

    for fmt in _MONTH_DAY_YEAR_FORMATS:
        try:
            parsed = datetime.strptime(title_cased, fmt)
        except ValueError:
            continue
        return date(parsed.year, parsed.month, parsed.day)

    candidate_with_year = f"{title_cased} {today.year}"
    for fmt in _MONTH_DAY_FORMATS:
        try:
            parsed = datetime.strptime(candidate_with_year, fmt)
        except ValueError:
            continue
        return date(today.year, parsed.month, parsed.day)

    return None
