"""The journal as tables, for analysis outside the bot.

Built from the journal on demand, never stored — like the trail. One row
per day and one row per meal, as CSV text the bot sends as files, ready for
pandas or a spreadsheet. Nutrition numbers are the extractor's rough
estimates, not measurements; other sources (heart rate, sleep) can be
joined on `date` later.
"""

from __future__ import annotations

import csv
import io
import re
from collections import defaultdict
from datetime import date
from typing import Any

from daylog import health, trail
from daylog.reconcile import note_count
from daylog.review import canonical_type
from daylog.vault import JournalEntry

NUTRIENTS = ("kcal", "protein_g", "carbs_g", "fat_g")
SCORES = ("energy", "mood", "focus")
DAY_COLUMNS = (
    "date",
    "place",
    "notes",
    "meals",
    "meals_estimated",
    *NUTRIENTS,
    *SCORES,
    "activities",
)
MEAL_COLUMNS = ("date", "when", "items", "place", "cost", "verdict", *NUTRIENTS)


def _number(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    return float(value)


def _mean(values: list[float]) -> float | None:
    return round(sum(values) / len(values), 2) if values else None


def _dicts(items: Any) -> list[dict[str, Any]]:
    return [i for i in items or [] if isinstance(i, dict)]


def _place(frontmatter: dict[str, Any]) -> str:
    location = frontmatter.get("location")
    if isinstance(location, dict):
        return str(location.get("place") or "")
    return str(location or "")


def nutrition_totals(meals: Any) -> dict[str, float | None]:
    """Sum of each nutrient over the meals that carry an estimate for it."""
    totals: dict[str, float | None] = {}
    for key in NUTRIENTS:
        values = [n for m in _dicts(meals) if (n := _number(m.get(key))) is not None]
        totals[key] = round(sum(values)) if values else None
    return totals


def score_means(felt: Any) -> dict[str, float | None]:
    return {
        key: _mean([n for f in _dicts(felt) if (n := _number(f.get(key))) is not None])
        for key in SCORES
    }


def day_row(day: date, entry: JournalEntry) -> dict[str, Any]:
    fm = entry.frontmatter
    meals = _dicts(fm.get("meals"))
    activities = _dicts(fm.get("activities"))
    hours: dict[str, float] = defaultdict(float)
    for a in activities:
        n = _number(a.get("hours"))
        if n is not None:
            kind = re.sub(r"[^a-z0-9]+", "_", canonical_type(str(a.get("type", "other"))))
            hours[f"h_{kind.strip('_') or 'other'}"] += n
    return {
        "date": day.isoformat(),
        "place": _place(fm),
        "notes": note_count(entry),
        "meals": len(meals),
        "meals_estimated": sum(1 for m in meals if _number(m.get("kcal")) is not None),
        **nutrition_totals(meals),
        **score_means(fm.get("felt")),
        "activities": len(activities),
        **{k: round(v, 2) for k, v in hours.items()},
    }


def day_rows(
    entries: dict[date, JournalEntry], location_data: Any = None, health_data: Any = None
) -> list[dict[str, Any]]:
    """One row per logged day; `place` falls back to the location trail.

    Phone measurements for the day, if any, are added as `health_*` columns.
    """
    all_stays = trail.stays(location_data) if location_data else []
    rows = []
    for day, entry in sorted(entries.items()):
        row = day_row(day, entry)
        if not row["place"]:
            stay = trail.stay_on(all_stays, day)
            row["place"] = stay.place if stay else ""
        row.update({f"health_{k}": v for k, v in health.for_day(health_data, day).items()})
        rows.append(row)
    return rows


def meal_rows(entries: dict[date, JournalEntry]) -> list[dict[str, Any]]:
    rows = []
    for day, entry in sorted(entries.items()):
        for m in _dicts(entry.frontmatter.get("meals")):
            items = m.get("items")
            rows.append(
                {
                    "date": day.isoformat(),
                    "when": m.get("when", ""),
                    "items": "; ".join(str(i) for i in items)
                    if isinstance(items, list)
                    else str(items or ""),
                    "place": m.get("place") or m.get("place_mention") or "",
                    "cost": m.get("cost", ""),
                    "verdict": m.get("verdict", ""),
                    **{key: _number(m.get(key)) for key in NUTRIENTS},
                }
            )
    return rows


def to_csv(rows: list[dict[str, Any]], lead: tuple[str, ...]) -> str:
    """CSV with `lead` columns first, then any others (activity hours) sorted."""
    extra = sorted({key for row in rows for key in row} - set(lead))
    buffer = io.StringIO()
    writer = csv.DictWriter(buffer, fieldnames=[*lead, *extra], restval="")
    writer.writeheader()
    for row in rows:
        writer.writerow({k: ("" if v is None else v) for k, v in row.items()})
    return buffer.getvalue()


def day_summary(frontmatter: dict[str, Any]) -> str | None:
    """One line of the day's rough food totals, or None without estimates."""
    totals = nutrition_totals(frontmatter.get("meals"))
    if totals["kcal"] is None:
        return None
    parts = [f"~{totals['kcal']:,.0f} kcal"]
    if totals["protein_g"] is not None:
        parts.append(f"{totals['protein_g']:.0f} g protein")
    return "Food (rough): " + ", ".join(parts)
