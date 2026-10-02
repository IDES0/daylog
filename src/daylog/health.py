"""Daily numbers sent from the phone (steps, sleep, ...), kept as reference.

An iOS Shortcut posts one small JSON object a day to the ingest endpoint in
calendar_server. This module validates it and merges it into `health.yaml`
in the vault (date -> {metric: number}); export and the status page join it
to the journal by date. The journal stays the record of what happened —
these are measurements beside it.
"""

from __future__ import annotations

import json
import re
from datetime import date, datetime
from typing import Any

FILE = "health.yaml"
MAX_BODY_BYTES = 4096
MAX_METRICS = 24
_KEY = re.compile(r"^[a-z][a-z0-9_]{0,31}$")
_NUMBER = re.compile(r"-?\d+(?:\.\d+)?")


class HealthError(ValueError):
    pass


def _number(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int | float):
        return float(value)
    if isinstance(value, str):
        # Shortcuts often sends a number with its unit ("8,423 steps", "1.9 mi").
        match = _NUMBER.search(value.replace(",", ""))
        return float(match.group()) if match else None
    return None


def _day(value: Any, default_day: date) -> date:
    """The posted date, in ISO or a few common phone formats; else the default."""
    text = str(value or "").strip()
    try:
        return date.fromisoformat(text[:10])
    except ValueError:
        pass
    for fmt in ("%d/%m/%Y", "%m/%d/%Y", "%b %d, %Y", "%d %b %Y", "%B %d, %Y"):
        try:
            return datetime.strptime(text.split(" at ")[0], fmt).date()
        except ValueError:
            continue
    return default_day


def parse(body: bytes, default_day: date) -> tuple[date, dict[str, float]]:
    """Validate a posted body into (day, metrics). Raises HealthError."""
    if len(body) > MAX_BODY_BYTES:
        raise HealthError("body too large")
    try:
        data = json.loads(body)
    except (ValueError, UnicodeDecodeError) as exc:
        raise HealthError("not JSON") from exc
    if not isinstance(data, dict):
        raise HealthError("expected a JSON object")

    day = _day(data.get("date"), default_day)

    metrics: dict[str, float] = {}
    for key, value in data.items():
        name = str(key).strip().lower().replace(" ", "_")
        number = _number(value)
        if name == "date" or number is None or not _KEY.match(name):
            continue
        metrics[name] = round(number, 2)
    if not metrics:
        raise HealthError(f"no numeric metrics in fields: {', '.join(map(str, data)) or 'none'}")
    if len(metrics) > MAX_METRICS:
        raise HealthError("too many metrics")
    return day, metrics


def merge(existing: Any, day: date, metrics: dict[str, float]) -> dict[str, dict[str, float]]:
    """The stored mapping with `day` updated (a later post that day wins per metric)."""
    merged = {str(k): dict(v) for k, v in (existing or {}).items() if isinstance(v, dict)}
    merged[day.isoformat()] = {**merged.get(day.isoformat(), {}), **metrics}
    return dict(sorted(merged.items()))


def for_day(stored: Any, day: date) -> dict[str, float]:
    row = (stored or {}).get(day.isoformat()) or (stored or {}).get(day)
    return dict(row) if isinstance(row, dict) else {}
