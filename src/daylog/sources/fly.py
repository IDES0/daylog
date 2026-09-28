"""Paragliding flyability per site, hour by hour, from Open-Meteo.

The surf engine's twin for the air. A `fly_site` place node carries a
`fly:` profile:

    fly:
      launch: [135, 225]     # wind directions (FROM) the launch faces into
      elevation: 1600        # launch altitude, metres
      max_wind: 12           # knots at launch height the site handles
      max_gust: 18           # knots

and the user's own rules (profile.yaml `fly_rules`, e.g. max_gust: 15,
max_gust_spread: 8, max_precip_prob: 30) override the site when stricter —
decided calmly in advance, so they hold when a better pilot is launching.

Wind at launch height comes from the 850 hPa level (~1,500 m), close to
typical Himalayan-foothill launches; 10 m wind is for landing. Cloud base
is estimated from the surface temperature/dew-point spread (~125 m per
°C); CAPE is used as a rough thermal-strength and overdevelopment signal.
Model guidance only — the school and the sky on the day decide.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime
from typing import Any

import httpx

from daylog.sources.surf import angle_diff, compass

logger = logging.getLogger(__name__)

URL = "https://api.open-meteo.com/v1/forecast"
FIELDS = (
    "wind_speed_10m,wind_gusts_10m,wind_direction_10m,wind_speed_850hPa,wind_direction_850hPa,"
    "precipitation_probability,cape,temperature_2m,dew_point_2m,cloud_cover_low"
)
FLYING_HOURS = range(9, 17)


@dataclass
class FlyProfile:
    launch: tuple[float, float]
    elevation: float
    max_wind: float
    max_gust: float
    max_gust_spread: float = 8.0
    max_precip_prob: float = 30.0

    @classmethod
    def from_node(cls, node: Any, rules: dict[str, Any] | None = None) -> FlyProfile | None:
        raw = node.get("fly") if node else None
        if not isinstance(raw, dict) or "launch" not in raw:
            return None
        rules = rules or {}
        lo, hi = (float(x) for x in raw["launch"])
        return cls(
            launch=(lo, hi),
            elevation=float(raw.get("elevation", 0)),
            max_wind=min(float(raw.get("max_wind", 12)), float(rules.get("max_wind", 99))),
            max_gust=min(float(raw.get("max_gust", 18)), float(rules.get("max_gust", 99))),
            max_gust_spread=float(rules.get("max_gust_spread", 8)),
            max_precip_prob=float(rules.get("max_precip_prob", 30)),
        )


@dataclass
class AirHour:
    time: datetime
    wind_launch: float
    dir_launch: float
    wind_10m: float
    gust_10m: float
    precip_prob: float
    cape: float
    spread_c: float  # temperature - dew point
    low_cloud: float


def parse(data: dict[str, Any]) -> list[AirHour]:
    h = data.get("hourly") or {}
    out = []
    for i, t in enumerate(h.get("time") or []):

        def v(key: str, default: float = 0.0, i: int = i) -> float:
            vals = h.get(key) or []
            val = vals[i] if i < len(vals) else None
            return float(val) if val is not None else default

        out.append(
            AirHour(
                time=datetime.fromisoformat(t),
                wind_launch=v("wind_speed_850hPa", v("wind_speed_10m")),
                dir_launch=v("wind_direction_850hPa", v("wind_direction_10m")),
                wind_10m=v("wind_speed_10m"),
                gust_10m=v("wind_gusts_10m"),
                precip_prob=v("precipitation_probability"),
                cape=v("cape"),
                spread_c=v("temperature_2m") - v("dew_point_2m"),
                low_cloud=v("cloud_cover_low"),
            )
        )
    return out


def _launch_ok(h: AirHour, p: FlyProfile) -> bool:
    if h.wind_launch < 4:
        return True  # nil wind: thermal-driven, launch direction matters little
    lo, hi = p.launch
    return (h.dir_launch - lo) % 360 <= (hi - lo) % 360 or angle_diff(h.dir_launch, lo) < 20


# Below this at launch height, 10 m "gusts" in mountain terrain are thermal
# turbulence in the model, not mechanical wind — reported, not a blocker.
MECHANICAL_WIND = 6.0


def reasons_not_flyable(h: AirHour, p: FlyProfile) -> list[tuple[str, str]]:
    """(key, human text) for every rule this hour breaks."""
    reasons = []
    if h.precip_prob > p.max_precip_prob:
        reasons.append(("rain", f"rain {h.precip_prob:.0f}%"))
    if h.wind_launch > p.max_wind:
        reasons.append(("wind", f"wind {h.wind_launch:.0f}kn aloft"))
    if h.wind_launch >= MECHANICAL_WIND:
        if h.gust_10m > p.max_gust:
            reasons.append(("gusts", f"gusts {h.gust_10m:.0f}kn"))
        if h.gust_10m - h.wind_10m > p.max_gust_spread:
            reasons.append(("gust-spread", f"gust spread {h.gust_10m - h.wind_10m:.0f}kn"))
    if not _launch_ok(h, p):
        reasons.append(("direction", f"cross/over-the-back {compass(h.dir_launch)}"))
    if h.cape > 1500 and h.precip_prob >= 10:
        reasons.append(("overdevelopment", "overdevelopment risk"))
    if h.low_cloud > 80:
        reasons.append(("cloud", "low cloud"))
    return reasons


def cloud_base(h: AirHour) -> float:
    """Metres above sea level — rough, from the surface T/Td spread."""
    return 125 * max(h.spread_c, 0)


def thermals(h: AirHour) -> str:
    if h.cape < 100:
        return "weak"
    if h.cape < 600:
        return "moderate"
    if h.cape <= 1500:
        return "strong"
    return "strong, OD risk"


def day_line(hours: list[AirHour], p: FlyProfile, site_ground: float = 0.0) -> str:
    flyable = [h for h in hours if not reasons_not_flyable(h, p)]
    day = hours[0].time.strftime("%a %d")
    if not flyable:
        blockers: dict[str, int] = {}
        for h in hours:
            for key, _text in reasons_not_flyable(h, p):
                blockers[key] = blockers.get(key, 0) + 1
        top = ", ".join(sorted(blockers, key=lambda k: -blockers[k])[:2])
        return f"{day}: NOT FLYABLE ({top})"
    start, end = flyable[0].time.hour, flyable[-1].time.hour + 1
    mid = flyable[len(flyable) // 2]
    base = site_ground + cloud_base(mid)
    headroom = base - p.elevation
    base_note = (
        f"base ~{base:,.0f}m ({headroom:+,.0f}m over launch)"
        if headroom > -500
        else "base below launch"
    )
    return (
        f"{day}: FLYABLE {start:02d}-{end:02d} ({len(flyable)}h) · {mid.wind_launch:.0f}kn "
        f"{compass(mid.dir_launch)} aloft · gusts {max(h.gust_10m for h in flyable):.0f}kn · "
        f"thermals {thermals(mid)} · {base_note}"
    )


def site_report(name: str, profile: FlyProfile, hours: list[AirHour], ground: float) -> str:
    by_day: dict[str, list[AirHour]] = {}
    for h in hours:
        if h.time.hour in FLYING_HOURS:
            by_day.setdefault(h.time.date().isoformat(), []).append(h)
    return "\n".join([f"{name}:"] + [f"  {day_line(d, profile, ground)}" for d in by_day.values()])


def fetch(lat: float, lon: float, timezone: str, days: int = 3) -> dict[str, Any] | None:
    try:
        r = httpx.get(
            URL,
            params={
                "latitude": lat,
                "longitude": lon,
                "hourly": FIELDS,
                "wind_speed_unit": "kn",
                "timezone": timezone,
                "forecast_days": days,
            },
            timeout=15.0,
        )
        r.raise_for_status()
    except httpx.HTTPError:
        logger.exception("fly forecast request failed for lat=%s lon=%s", lat, lon)
        return None
    return r.json()  # type: ignore[no-any-return]


def report(
    sites: list[Any], timezone: str, rules: dict[str, Any] | None = None, days: int = 3
) -> str | None:
    blocks = []
    for node in sites:
        profile = FlyProfile.from_node(node, rules)
        if profile is None or node.get("lat") is None or node.get("lon") is None:
            continue
        data = fetch(float(node["lat"]), float(node["lon"]), timezone, days)
        if not data:
            continue
        ground = float(data.get("elevation") or 0)
        blocks.append(site_report(str(node.get("name")), profile, parse(data), ground))
    if not blocks:
        return None
    return (
        "Paragliding flyability from Open-Meteo model data, with the user's own no-fly "
        "rules applied. Guidance only — the school and the sky decide on the day.\n\n"
        + "\n\n".join(blocks)
    )
