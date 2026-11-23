"""Health trends from the measured data, computed on demand, never stored.

Pure logic over health.yaml (daily numbers), the per-day files
(health/days/*.yaml) and the journal's own scores (energy, mood, focus).
It records facts and their drift against the user's own baseline — the
last 7 days against the 28 before them — and how two series move
together. No readiness score and no grade for a day: the design rules
don't score days.

Used by the web app's Health view (`GET /api/health`), the calendar's
per-day numbers, and as short text in the brief and the weekly review.
"""

from __future__ import annotations

import math
from collections import defaultdict
from collections.abc import Mapping
from datetime import date, datetime, timedelta
from typing import Any

from daylog import health, measures

# key -> (label, unit, health.yaml names to read it from, best first)
DAILY: dict[str, tuple[str, str, tuple[str, ...]]] = {
    "sleep_h": ("Sleep", "h", ("sleep_h",)),
    "sleep_deep_h": ("Deep sleep", "h", ("sleep_deep_h",)),
    "sleep_rem_h": ("REM sleep", "h", ("sleep_rem_h",)),
    "resting_hr": ("Resting heart rate", "bpm", ("resting_heart_rate", "resting_hr")),
    "hrv": ("HRV", "ms", ("heart_rate_variability", "hrv")),
    "steps": ("Steps", "", ("step_count", "steps")),
    "active_kcal": ("Active energy", "kcal", ("active_energy", "active_kcal")),
    "exercise_min": ("Exercise", "min", ("apple_exercise_time", "exercise_min")),
    "stand_h": ("Stand hours", "h", ("apple_stand_hour",)),
    "daylight_min": ("Time in daylight", "min", ("time_in_daylight",)),
    "vo2max": ("VO₂ max", "", ("vo2_max",)),
    "resp_rate": ("Respiratory rate", "/min", ("respiratory_rate",)),
    "spo2": ("Blood oxygen", "%", ("blood_oxygen_saturation",)),
    "wrist_temp": ("Sleeping wrist temp.", "°", ("apple_sleeping_wrist_temperature",)),
    "weight": ("Weight", "", ("body_mass", "weight")),
}
# From the day files and the journal.
DERIVED: dict[str, tuple[str, str]] = {
    "bed_h": ("Fell asleep", "clock"),
    "wake_h": ("Woke", "clock"),
    "workout_min": ("Workouts", "min"),
    "screen_min": ("Screen time", "min"),
    "screen_mac_min": ("Mac screen time", "min"),
    "screen_iphone_min": ("iPhone screen time", "min"),
    "energy": ("Energy (journal)", "/5"),
    "mood": ("Mood (journal)", "/5"),
    "focus": ("Focus (journal)", "/5"),
}
# Pairs worth a glance: (x, y, shift). shift=1 pairs x on day d with y on d+1
# (screen tonight -> the sleep filed under tomorrow).
PAIRS: tuple[tuple[str, str, int], ...] = (
    ("sleep_h", "energy", 0),
    ("sleep_h", "mood", 0),
    ("sleep_h", "focus", 0),
    ("hrv", "energy", 0),
    ("workout_min", "mood", 0),
    ("steps", "mood", 0),
    ("daylight_min", "mood", 0),
    ("screen_min", "mood", 0),
    ("screen_min", "sleep_h", 1),
    ("workout_min", "sleep_h", 1),
    ("bed_h", "energy", 1),
)
MIN_PAIRS = 10
BASELINE_RECENT = 7
BASELINE_BEFORE = 28


def _num(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    return float(value) if math.isfinite(value) else None


def main_sleep(records: Mapping[str, Any] | None) -> dict[str, Any] | None:
    """The night's main sleep (the longest record), not a nap."""
    nights = [dict(n) for n in (records or {}).get("sleep") or [] if isinstance(n, Mapping)]
    if not nights:
        return None

    def length(n: dict[str, Any]) -> float:
        a, b = health.wall(n.get("start")), health.wall(n.get("end"))
        return (b - a).total_seconds() if a and b else 0.0

    return max(nights, key=length)


def day_numbers(
    daily: Mapping[str, Any] | None, records: Mapping[str, Any] | None
) -> dict[str, Any]:
    """One day's measured numbers, under the keys of DAILY and DERIVED.

    `bed_h` is when sleep began, in hours from the morning's midnight
    (23:30 is -0.5), so nights average sensibly; `bed` / `wake` are clock text."""
    daily = daily or {}
    out: dict[str, Any] = {}
    for key, (_, _, names) in DAILY.items():
        for name in names:
            if (n := _num(daily.get(name))) is not None:
                out[key] = round(n, 2)
                break
    night = main_sleep(records)
    if night:
        a, b = health.wall(night.get("start")), health.wall(night.get("end"))
        if a and b:
            midnight = datetime.combine(b.date(), datetime.min.time())
            out["bed_h"] = round((a - midnight).total_seconds() / 3600, 2)
            out["wake_h"] = round((b - midnight).total_seconds() / 3600, 2)
            out["bed"], out["wake"] = f"{a:%H:%M}", f"{b:%H:%M}"
        if "sleep_h" not in out and (n := _num(night.get("asleep_h"))) is not None:
            out["sleep_h"] = n
    workouts = [w for w in (records or {}).get("workouts") or [] if isinstance(w, Mapping)]
    if workouts:
        out["workout_min"] = round(sum(_num(w.get("minutes")) or 0 for w in workouts))
        out["workouts"] = len(workouts)
    screen = measures.screen_minutes(dict(records or {}))
    if screen:
        for device, apps in screen.items():
            if device in ("mac", "iphone"):
                out[f"screen_{device}_min"] = round(sum(apps.values()))
        out["screen_min"] = round(sum(sum(apps.values()) for apps in screen.values()))
    return out


def journal_scores(frontmatter: Mapping[str, Any] | None) -> dict[str, float]:
    """The day's energy, mood and focus from `felt` (averaged when told more than once)."""
    scores: dict[str, list[float]] = defaultdict(list)
    for f in (frontmatter or {}).get("felt") or []:
        if not isinstance(f, Mapping):
            continue
        for key in ("energy", "mood", "focus"):
            if (n := _num(f.get(key))) is not None:
                scores[key].append(n)
    return {k: round(sum(v) / len(v), 2) for k, v in scores.items()}


def series(
    days: list[date],
    numbers: Mapping[str, Any],
    records: Mapping[date, Mapping[str, Any]],
    journal: Mapping[date, Mapping[str, Any]],
) -> list[dict[str, Any]]:
    """One row per day: measured numbers, workouts, screen and the journal's scores."""
    rows = []
    for d in days:
        row: dict[str, Any] = {"date": d.isoformat()}
        row.update(day_numbers(health.for_day(numbers, d), records.get(d)))
        row.update(journal_scores(journal.get(d)))
        rows.append(row)
    return rows


def _mean(values: list[float]) -> float | None:
    return sum(values) / len(values) if values else None


def baselines(rows: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """Per key: the last 7 days' average against the 28 days before them."""
    recent, before = (
        rows[-BASELINE_RECENT:],
        rows[-BASELINE_RECENT - BASELINE_BEFORE : -BASELINE_RECENT],
    )
    out = {}
    for key in [*DAILY, *DERIVED]:
        r = [v for row in recent if (v := _num(row.get(key))) is not None]
        b = [v for row in before if (v := _num(row.get(key))) is not None]
        if not r:
            continue
        now, base = _mean(r), _mean(b) if len(b) >= 7 else None
        assert now is not None
        out[key] = {
            "recent": round(now, 2),
            "baseline": None if base is None else round(base, 2),
            "change": None if base is None else round(now - base, 2),
            "days": len(r),
        }
    return out


def pearson(xs: list[float], ys: list[float]) -> float | None:
    n = len(xs)
    if n < 3:
        return None
    mx, my = sum(xs) / n, sum(ys) / n
    sxx = sum((x - mx) ** 2 for x in xs)
    syy = sum((y - my) ** 2 for y in ys)
    if sxx == 0 or syy == 0:
        return None
    return sum((x - mx) * (y - my) for x, y in zip(xs, ys, strict=True)) / math.sqrt(sxx * syy)


def strength(r: float) -> str:
    a = abs(r)
    word = "no clear" if a < 0.2 else "weak" if a < 0.4 else "moderate" if a < 0.6 else "strong"
    return word if word == "no clear" else f"{word} {'positive' if r > 0 else 'negative'}"


def correlations(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Each pair in PAIRS with enough days in common: r, n and a plain word.

    A glance, not a finding: with a few weeks of one person's days, r says
    two things moved together, never why."""
    by_day = {row["date"]: row for row in rows}
    out = []
    for x, y, shift in PAIRS:
        xs, ys = [], []
        for row in rows:
            other = by_day.get(
                (date.fromisoformat(row["date"]) + timedelta(days=shift)).isoformat()
            )
            vx, vy = _num(row.get(x)), _num((other or {}).get(y))
            if vx is not None and vy is not None:
                xs.append(vx)
                ys.append(vy)
        if len(xs) < MIN_PAIRS or (r := pearson(xs, ys)) is None:
            continue
        out.append(
            {
                "x": x,
                "y": y,
                "next_day": bool(shift),
                "r": round(r, 2),
                "n": len(xs),
                "word": strength(r),
            }
        )
    return sorted(out, key=lambda c: -abs(c["r"]))


def workout_totals(records: Mapping[date, Mapping[str, Any]]) -> list[dict[str, Any]]:
    """Per workout kind over the range: sessions, minutes, kcal."""
    totals: dict[str, dict[str, float]] = defaultdict(lambda: defaultdict(float))
    for rec in records.values():
        for w in rec.get("workouts") or []:
            if not isinstance(w, Mapping):
                continue
            t = totals[str(w.get("kind") or "?")]
            t["sessions"] += 1
            t["minutes"] += _num(w.get("minutes")) or 0
            t["kcal"] += _num(w.get("kcal")) or 0
    return sorted(
        (
            {
                "kind": k,
                "sessions": int(v["sessions"]),
                "minutes": round(v["minutes"]),
                "kcal": round(v["kcal"]),
            }
            for k, v in totals.items()
        ),
        key=lambda t: -t["minutes"],
    )


def screen_totals(records: Mapping[date, Mapping[str, Any]], top: int = 12) -> dict[str, Any]:
    """Per device over the range: minutes, days measured and the top apps or sites."""
    per: dict[str, dict[str, float]] = defaultdict(lambda: defaultdict(float))
    days: dict[str, int] = defaultdict(int)
    for rec in records.values():
        for device, apps in measures.screen_minutes(dict(rec)).items():
            days[device] += 1
            for app, m in apps.items():
                per[device][app] += m
    return {
        device: {
            "minutes": round(sum(apps.values())),
            "days": days[device],
            "per_day": round(sum(apps.values()) / max(days[device], 1)),
            "top": [
                {"app": a, "minutes": round(m)}
                for a, m in sorted(apps.items(), key=lambda kv: -kv[1])[:top]
            ],
        }
        for device, apps in per.items()
    }


# -- text for the brief, the review and chat ----------------------------------------------


def duration(minutes: float) -> str:
    m = round(minutes)
    return f"{m // 60} h {m % 60:02d}" if m >= 60 else f"{m} min"


def _clock(hours: float) -> str:
    m = round(hours * 60) % (24 * 60)
    return f"{m // 60:02d}:{m % 60:02d}"


def _vs(key: str, base: Mapping[str, Any], unit: str = "", digits: int = 0) -> str:
    b = base.get(key) or {}
    if b.get("baseline") is None:
        return ""
    return f" (28-day {b['baseline']:.{digits}f}{unit})"


def brief_text(
    today: date, rows: list[dict[str, Any]], records: Mapping[date, Mapping[str, Any]]
) -> str:
    """A few lines on last night, yesterday and the drift from baseline; '' if nothing measured."""
    by_day = {row["date"]: row for row in rows}
    base = baselines([r for r in rows if r["date"] < today.isoformat()])
    night, yday = (
        by_day.get(today.isoformat(), {}),
        by_day.get((today - timedelta(days=1)).isoformat(), {}),
    )
    lines = []
    if night.get("sleep_h") is not None:
        bits = [f"slept {night['sleep_h']:.1f} h"]
        if night.get("bed"):
            bits[0] += f" ({night['bed']}–{night['wake']})"
        stages = [
            f"{k} {night[f'sleep_{k}_h']:.1f}" for k in ("deep", "rem") if night.get(f"sleep_{k}_h")
        ]
        if stages:
            bits.append(", ".join(stages).replace("rem", "REM"))
        b = base.get("sleep_h") or {}
        if b.get("recent") is not None:
            bits.append(f"7-day avg {b['recent']:.1f} h" + _vs("sleep_h", base, " h", 1))
        lines.append("Last night: " + "; ".join(bits) + ".")
    heart = []
    for key, label, unit in (("resting_hr", "resting HR", " bpm"), ("hrv", "HRV", " ms")):
        value = night.get(key, yday.get(key))
        if value is not None:
            heart.append(f"{label} {value:.0f}{unit}" + _vs(key, base, unit))
    if heart:
        lines.append("Heart: " + "; ".join(heart) + ".")
    day_bits = []
    rec = records.get(today - timedelta(days=1)) or {}
    workouts = [w for w in rec.get("workouts") or [] if isinstance(w, Mapping)]
    if workouts:
        kinds = ", ".join(
            f"{w.get('kind')} {duration(_num(w.get('minutes')) or 0)}" for w in workouts
        )
        day_bits.append(f"{len(workouts)} workout{'s' * (len(workouts) > 1)} ({kinds})")
    if yday.get("steps") is not None:
        day_bits.append(f"{yday['steps']:,.0f} steps")
    screen = measures.screen_minutes(dict(rec))
    for device, apps in screen.items():
        top = ", ".join(f"{a} {duration(m)}" for a, m in list(apps.items())[:3])
        day_bits.append(f"{device} screen {duration(sum(apps.values()))} ({top})")
    if day_bits:
        lines.append("Yesterday: " + "; ".join(day_bits) + ".")
    return "\n".join(lines)


def review_text(rows: list[dict[str, Any]], records: Mapping[date, Mapping[str, Any]]) -> str:
    """The week in measured numbers, for the weekly review; '' if nothing measured."""
    week = rows[-7:]
    lines = []
    for key in (
        "sleep_h",
        "bed_h",
        "wake_h",
        "resting_hr",
        "hrv",
        "steps",
        "exercise_min",
        "screen_min",
    ):
        values = [v for row in week if (v := _num(row.get(key))) is not None]
        if not values:
            continue
        label, unit = (DAILY.get(key) or (*DERIVED[key], ()))[:2]
        avg = sum(values) / len(values)
        shown = (
            _clock(avg)
            if unit == "clock"
            else duration(avg)
            if unit == "min"
            else f"{avg:.1f}{' ' + unit if unit else ''}"
        )
        lines.append(f"- {label}: avg {shown} over {len(values)} days")
    for t in workout_totals({d: r for d, r in records.items()})[:8]:
        lines.append(f"- {t['kind']}: {t['sessions']}× · {duration(t['minutes'])}")
    return "\n".join(lines)
