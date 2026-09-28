"""Spot-by-spot surf ratings, hour by hour — a small Surfline, from Open-Meteo.

The old forecast was one daily maximum per spot, and every spot in a
region sat in the same ~5-8 km model cell, so the brief saw the same
numbers three times and had to guess what they meant. This rates each
spot against what *that* spot needs, using a `surf:` profile on its
place node:

    surf:
      swell: [170, 250]     # usable swell window (directions it comes FROM)
      best_swell: 225       # best swell direction
      offshore: 100         # wind-from direction that's straight offshore
      tide: [mid, high]     # tide states it works on
      size: [0.8, 2.5]      # deep-water swell height it handles, metres
      min_period: 9         # below this the swell is weak/windswell for this spot

Each daylight hour gets a 0-5 rating from swell size, period, direction,
wind relative to offshore, and tide state, and each day is summarised as
its best window. Numbers are model output, not a surf report — the tide
curve especially is approximate (Open-Meteo's own caveat) — so the text
says "model" and the brief treats it as guidance.

Pure apart from `fetch_hourly`; everything else is testable offline.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime
from typing import Any

import httpx

logger = logging.getLogger(__name__)

MARINE_URL = "https://marine-api.open-meteo.com/v1/marine"
WIND_URL = "https://api.open-meteo.com/v1/forecast"
MARINE_FIELDS = (
    "swell_wave_height,swell_wave_period,swell_wave_direction,"
    "secondary_swell_wave_height,secondary_swell_wave_period,secondary_swell_wave_direction,"
    "wind_wave_height,sea_level_height_msl"
)
WIND_FIELDS = "wind_speed_10m,wind_direction_10m,wind_gusts_10m"
DAYLIGHT = range(6, 19)  # local hours worth surfing
# Surfline-style wording; EPIC is meant to be rare.
LABELS = (
    (4.6, "EPIC"),
    (4.0, "VERY GOOD"),
    (3.25, "GOOD"),
    (2.5, "FAIR-GOOD"),
    (1.75, "FAIR"),
    (1.0, "POOR-FAIR"),
    (0.0, "POOR"),
)
COMPASS = (
    "N",
    "NNE",
    "NE",
    "ENE",
    "E",
    "ESE",
    "SE",
    "SSE",
    "S",
    "SSW",
    "SW",
    "WSW",
    "W",
    "WNW",
    "NW",
    "NNW",
)


@dataclass
class SurfProfile:
    swell: tuple[float, float]
    best_swell: float
    offshore: float
    tide: tuple[str, ...]
    size: tuple[float, float]
    min_period: float

    @classmethod
    def from_node(cls, node: Any) -> SurfProfile | None:
        raw = node.get("surf") if node else None
        if not isinstance(raw, dict) or "swell" not in raw or "offshore" not in raw:
            return None
        lo, hi = (float(x) for x in raw["swell"])
        return cls(
            swell=(lo, hi),
            best_swell=float(raw.get("best_swell", _mid_angle(lo, hi))),
            offshore=float(raw["offshore"]),
            tide=tuple(str(t) for t in raw.get("tide", ("low", "mid", "high"))),
            size=tuple(float(x) for x in raw.get("size", (0.6, 3.0))),  # type: ignore[arg-type]
            min_period=float(raw.get("min_period", 8)),
        )


@dataclass
class Hour:
    time: datetime
    swell_h: float
    swell_t: float
    swell_dir: float
    wind_kn: float
    wind_dir: float
    gust_kn: float
    tide: str  # low / mid / high
    tide_trend: str  # rising / falling
    rating: float = 0.0


def compass(deg: float) -> str:
    return COMPASS[int((deg % 360) / 22.5 + 0.5) % 16]


def angle_diff(a: float, b: float) -> float:
    d = abs(a - b) % 360
    return 360 - d if d > 180 else d


def _mid_angle(lo: float, hi: float) -> float:
    span = (hi - lo) % 360
    return (lo + span / 2) % 360


def _in_window(deg: float, lo: float, hi: float) -> bool:
    return (deg - lo) % 360 <= (hi - lo) % 360


def direction_factor(deg: float, p: SurfProfile) -> float:
    """1.0 at the best direction, ~0.5 at the window edge, fading to 0 30° outside it."""
    half = max(((p.swell[1] - p.swell[0]) % 360) / 2, 1.0)
    if _in_window(deg, *p.swell):
        return 1.0 - 0.5 * min(angle_diff(deg, p.best_swell) / half, 1.0)
    outside = min(angle_diff(deg, p.swell[0]), angle_diff(deg, p.swell[1]))
    return max(0.0, 0.5 * (1 - outside / 30))


def size_factor(height: float, p: SurfProfile) -> float:
    """Best from ~40% into the spot's range up to its top; the bottom of the
    range is surfable but small (0.7), below it fades, above it closes out."""
    lo, hi = p.size
    if height < 0.3:
        return 0.0
    if height < lo:
        return 0.7 * height / lo
    if height > hi:
        return max(0.0, 1 - (height - hi) / hi)
    return 0.7 + 0.3 * min((height - lo) / (0.4 * (hi - lo) or 1), 1.0)


def period_factor(period: float, p: SurfProfile) -> float:
    """Short-period swell is weak and messy on a reef: full marks only 4s+ above
    the spot's minimum, and it drops off fast below it."""
    if period >= p.min_period + 4:
        return 1.0
    if period >= p.min_period:
        return 0.75 + 0.25 * (period - p.min_period) / 4
    return max(0.2, 0.75 - 0.15 * (p.min_period - period))


def wind_factor(speed_kn: float, wind_from: float, p: SurfProfile) -> float:
    """Glassy or offshore is clean; cross-shore costs a little; onshore costs a lot."""
    if speed_kn < 5:
        return 1.0
    rel = angle_diff(wind_from, p.offshore)
    if rel <= 45:
        return 0.85 if speed_kn > 20 else 1.0
    if rel <= 110:
        return max(0.4, 1 - speed_kn / 30)
    return max(0.1, 1 - speed_kn / 14)


def rate(hour: Hour, p: SurfProfile) -> float:
    swell = size_factor(hour.swell_h, p) * period_factor(hour.swell_t, p)
    quality = (
        swell * direction_factor(hour.swell_dir, p) * wind_factor(hour.wind_kn, hour.wind_dir, p)
    )
    tide = 1.0 if hour.tide in p.tide else 0.6
    return round(5 * quality * tide, 1)


def label(rating: float, height: float) -> str:
    if height < 0.3:
        return "FLAT"
    return next(name for floor, name in LABELS if rating >= floor)


def face_estimate(height: float, period: float) -> tuple[float, float]:
    """Rough breaking-face range from deep-water swell — longer period, bigger faces."""
    factor = min(max(1.0 + (period - 8) * 0.07, 0.8), 1.6)
    face = height * factor
    return round(face * 0.8, 1), round(face * 1.2, 1)


def _tide_states(levels: list[float | None], days: list[str]) -> list[tuple[str, str]]:
    """low/mid/high per hour, scaled to each day's own range; plus rising/falling."""
    out: list[tuple[str, str]] = []
    by_day: dict[str, list[float]] = {}
    for level, day in zip(levels, days, strict=True):
        if level is not None:
            by_day.setdefault(day, []).append(level)
    for i, (level, day) in enumerate(zip(levels, days, strict=True)):
        vals = by_day.get(day) or [0.0]
        lo, hi = min(vals), max(vals)
        if level is None or hi - lo < 0.05:
            out.append(("mid", "?"))
            continue
        frac = (level - lo) / (hi - lo)
        state = "low" if frac < 0.33 else "high" if frac > 0.67 else "mid"
        nxt = levels[i + 1] if i + 1 < len(levels) else None
        trend = "?" if nxt is None else ("rising" if nxt > level else "falling")
        out.append((state, trend))
    return out


def parse(marine: dict[str, Any], wind: dict[str, Any]) -> list[Hour]:
    """Join Open-Meteo marine + wind hourly JSON into Hours (same timezone, same grid)."""
    m, w = marine.get("hourly") or {}, wind.get("hourly") or {}
    times = m.get("time") or []
    wind_at = {t: i for i, t in enumerate(w.get("time") or [])}
    tides = _tide_states(
        m.get("sea_level_height_msl") or [None] * len(times), [t[:10] for t in times]
    )
    hours = []
    for i, t in enumerate(times):
        j = wind_at.get(t)
        h, per, d = (
            m.get(k, [None] * len(times))[i]
            for k in ("swell_wave_height", "swell_wave_period", "swell_wave_direction")
        )
        if h is None or per is None or d is None or j is None:
            continue
        # A bigger secondary swell (common on reefs picking up two swells) wins.
        h2 = (m.get("secondary_swell_wave_height") or [None] * len(times))[i]
        if h2 is not None and h2 > h:
            h = h2
            per = (m.get("secondary_swell_wave_period") or [per] * len(times))[i] or per
            d = (m.get("secondary_swell_wave_direction") or [d] * len(times))[i] or d
        hours.append(
            Hour(
                time=datetime.fromisoformat(t),
                swell_h=float(h),
                swell_t=float(per),
                swell_dir=float(d),
                wind_kn=float(w["wind_speed_10m"][j] or 0),
                wind_dir=float(w["wind_direction_10m"][j] or 0),
                gust_kn=float((w.get("wind_gusts_10m") or [0] * (j + 1))[j] or 0),
                tide=tides[i][0],
                tide_trend=tides[i][1],
            )
        )
    return hours


def best_window(hours: list[Hour]) -> list[Hour]:
    """The longest run of daylight hours within 0.5 of the day's best rating."""
    if not hours:
        return []
    top = max(h.rating for h in hours)
    best: list[Hour] = []
    run: list[Hour] = []
    for h in hours:
        if h.rating >= top - 0.5:
            run.append(h)
            if len(run) > len(best):
                best = list(run)
        else:
            run = []
    return best


def day_line(hours: list[Hour]) -> str:
    """'Tue 29: GOOD 06-09 · 1.4-2.0m faces @ 11s SSW · wind 2kn NE (offshore) · mid, rising'."""
    window = best_window(hours)
    if not window:
        return "no data"
    peak = max(window, key=lambda h: h.rating)
    lo, hi = face_estimate(peak.swell_h, peak.swell_t)
    start, end = window[0].time.hour, window[-1].time.hour + 1
    worst = min(hours, key=lambda h: h.rating)
    note = ""
    if worst.rating < peak.rating - 1.5:
        note = f"; {label(worst.rating, worst.swell_h).lower()} by {worst.time.hour:02d}:00"
    when = (
        f"{peak.time.strftime('%a %d')}: {label(peak.rating, peak.swell_h)} ({peak.rating:.1f}/5)"
    )
    swell = f"{lo}-{hi}m faces @ {peak.swell_t:.0f}s {compass(peak.swell_dir)}"
    wind = f"wind {peak.wind_kn:.0f}kn {compass(peak.wind_dir)}"
    tide = f"{peak.tide} tide {peak.tide_trend}"
    return f"{when} {start:02d}-{end:02d} · {swell} · {wind} · {tide}{note}"


def spot_report(
    name: str, profile: SurfProfile, hours: list[Hour], comfort_face: float | None = None
) -> str:
    """One line per day. `comfort_face` (metres, from the user's profile) adds a
    personal note when the best window is bigger than they're comfortable in —
    the rating stays about the spot, the note is about them."""
    by_day: dict[str, list[Hour]] = {}
    for h in hours:
        if h.time.hour in DAYLIGHT:
            h.rating = rate(h, profile)
            by_day.setdefault(h.time.date().isoformat(), []).append(h)
    lines = [f"{name}:"]
    for day_hours in by_day.values():
        line = day_line(day_hours)
        window = best_window(day_hours)
        if comfort_face and window:
            peak = max(window, key=lambda h: h.rating)
            if face_estimate(peak.swell_h, peak.swell_t)[1] > comfort_face:
                line += f" · above your {comfort_face:g}m comfort"
        lines.append(f"  {line}")
    return "\n".join(lines)


def fetch_hourly(
    lat: float, lon: float, timezone: str, days: int = 3
) -> tuple[dict[str, Any], dict[str, Any]] | None:
    """Raw hourly marine + wind JSON for one point, or None on any failure."""
    common: dict[str, Any] = {
        "latitude": lat,
        "longitude": lon,
        "timezone": timezone,
        "forecast_days": days,
    }
    try:
        marine = httpx.get(MARINE_URL, params={**common, "hourly": MARINE_FIELDS}, timeout=15.0)
        marine.raise_for_status()
        wind = httpx.get(
            WIND_URL,
            params={**common, "hourly": WIND_FIELDS, "wind_speed_unit": "kn"},
            timeout=15.0,
        )
        wind.raise_for_status()
    except httpx.HTTPError:
        logger.exception("surf forecast request failed for lat=%s lon=%s", lat, lon)
        return None
    return marine.json(), wind.json()


def report(
    spots: list[Any], timezone: str, days: int = 3, comfort_face: float | None = None
) -> str | None:
    """Rated forecast for every spot node that has a `surf:` profile and coordinates.

    Spots within ~2 km share one model cell, so they share one fetch — the
    ratings still differ because each spot's profile differs.
    """
    cache: dict[tuple[float, float], list[Hour] | None] = {}
    blocks = []
    for node in spots:
        profile = SurfProfile.from_node(node)
        if profile is None or node.get("lat") is None or node.get("lon") is None:
            continue
        key = (round(float(node["lat"]), 2), round(float(node["lon"]), 2))
        if key not in cache:
            raw = fetch_hourly(key[0], key[1], timezone, days)
            cache[key] = parse(*raw) if raw else None
        hours = cache[key]
        if hours:
            copies = [Hour(**{**h.__dict__}) for h in hours]
            blocks.append(spot_report(str(node.get("name")), profile, copies, comfort_face))
    if not blocks:
        return None
    return (
        "Rated per spot from Open-Meteo model data (swell, wind, approximate tide). "
        "Ratings are 0-5 against each spot's own profile.\n\n" + "\n\n".join(blocks)
    )
