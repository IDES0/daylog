"""The weekly review: what the user said they'd do vs what they did.

The numbers are computed here, deterministically — hours per activity,
goal deltas, meals logged, energy — so the model never does arithmetic
over journal text. The model gets those numbers plus the week's journal
and last week's review, and writes a short read of the week.
"""

from __future__ import annotations

import json
from collections import defaultdict
from datetime import date, timedelta
from pathlib import Path
from typing import Any

import anthropic

from daylog import llm
from daylog.vault import JournalEntry

PROMPT_PATH = Path(__file__).parent / "prompts" / "review.md"


def week_bounds(any_day: date) -> tuple[date, date]:
    """Monday..Sunday of the ISO week containing `any_day`."""
    start = any_day - timedelta(days=any_day.weekday())
    return start, start + timedelta(days=6)


def week_label(any_day: date) -> str:
    year, week, _ = any_day.isocalendar()
    return f"{year}-W{week:02d}"


# Labels extraction used before it had a fixed vocabulary, folded into the
# canonical ones so older weeks compare cleanly.
_SYNONYMS = {
    "scuba diving": "dive",
    "diving": "dive",
    "scuba": "dive",
    "job applications": "job_search",
    "job search": "job_search",
    "job_applications": "job_search",
    "socializing": "social",
    "networking": "social",
    "hiking": "hike",
    "ferry": "travel",
    "boat": "travel",
    "driving": "travel",
    "foiling": "foil",
    "work/distraction": "screen_time",
    "leisure": "rest",
    "relax": "rest",
}


def canonical_type(label: str) -> str:
    key = label.strip().lower()
    return _SYNONYMS.get(key, key)


def week_stats(entries: dict[date, JournalEntry]) -> dict[str, Any]:
    hours: dict[str, float] = defaultdict(float)
    counts: dict[str, int] = defaultdict(int)
    goal_deltas: dict[str, float] = defaultdict(float)
    energy: list[int] = []
    places: list[str] = []
    meals = 0
    skipped: list[str] = []
    for _, entry in sorted(entries.items()):
        fm = entry.frontmatter
        for a in fm.get("activities") or []:
            if not isinstance(a, dict):
                continue
            kind = canonical_type(str(a.get("type", "?")))
            counts[kind] += 1
            if isinstance(a.get("hours"), int | float):
                hours[kind] += float(a["hours"])
            if a.get("place") and a["place"] not in places:
                places.append(str(a["place"]))
        for g in fm.get("goal_progress") or []:
            if isinstance(g, dict) and isinstance(g.get("delta"), int | float):
                goal_deltas[str(g.get("goal_id"))] += float(g["delta"])
        for f in fm.get("felt") or []:
            if isinstance(f, dict) and isinstance(f.get("energy"), int):
                energy.append(f["energy"])
        meals += len(fm.get("meals") or [])
        skipped += [str(s) for s in fm.get("skipped") or []]
    return {
        "days_logged": len(entries),
        "activity_counts": dict(sorted(counts.items(), key=lambda kv: -kv[1])),
        "activity_hours": {k: round(v, 1) for k, v in sorted(hours.items(), key=lambda kv: -kv[1])},
        "goal_deltas": dict(goal_deltas),
        "avg_energy": round(sum(energy) / len(energy), 1) if energy else None,
        "meals_logged": meals,
        "places_visited": places,
        "skipped": skipped,
    }


def build_input(
    entries: dict[date, JournalEntry],
    stats: dict[str, Any],
    goals_lines: str,
    last_review: str | None,
    spend: str,
) -> str:
    days = []
    for d, entry in sorted(entries.items()):
        fm = {
            k: v for k, v in entry.frontmatter.items() if k in ("mood", "open_questions", "skipped")
        }
        heading = f"## {d.isoformat()} ({d.strftime('%A')})"
        days.append(f"{heading}\n{entry.summary}\n{json.dumps(fm, default=str)}")
    return (
        f"Goals:\n{goals_lines}\n\n"
        f"This week's numbers (computed, exact):\n{json.dumps(stats, default=str, indent=1)}\n\n"
        f"API spend: {spend}\n\n"
        f"Last week's review:\n{last_review or '(none — first review)'}\n\n"
        f"This week's journal:\n\n" + "\n\n".join(days)
    )


def write(review_input: str, client: anthropic.Anthropic | None = None) -> tuple[str, float]:
    client = client or anthropic.Anthropic()
    response = llm.create(
        client,
        model=llm.MAIN_MODEL,
        max_tokens=4000,
        system=PROMPT_PATH.read_text(encoding="utf-8"),
        messages=[{"role": "user", "content": review_input}],
        output_config={"effort": "high"},
    )
    cost = llm.record("review", llm.MAIN_MODEL, response.usage)
    text = "".join(b.text for b in response.content if b.type == "text").strip()
    return text, cost
