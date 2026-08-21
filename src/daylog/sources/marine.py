"""Swell/marine forecast via Open-Meteo — free, public, no API key.

https://marine-api.open-meteo.com — verified live against real coordinates
during development (Kuta, Lombok, ID).
"""

from __future__ import annotations

import logging

import httpx

logger = logging.getLogger(__name__)

_URL = "https://marine-api.open-meteo.com/v1/marine"
_DAILY_FIELDS = (
    "wave_height_max,wave_period_max,swell_wave_height_max,"
    "swell_wave_period_max,swell_wave_direction_dominant"
)


def fetch_forecast(lat: float, lon: float, timezone: str, days: int = 5) -> str | None:
    """Fetch a daily swell forecast, formatted as plain text for a prompt.

    Returns None on any request failure or if the location has no marine
    data (e.g. inland) — callers should treat that as "no forecast
    available," not raise. A brief is still useful without swell data.
    """
    try:
        response = httpx.get(
            _URL,
            params={
                "latitude": lat,
                "longitude": lon,
                "daily": _DAILY_FIELDS,
                "timezone": timezone,
                "forecast_days": days,
            },
            timeout=10.0,
        )
        response.raise_for_status()
        data = response.json()
    except httpx.HTTPError:
        logger.exception("marine forecast request failed for lat=%s lon=%s", lat, lon)
        return None

    daily = data.get("daily")
    if not daily or not daily.get("time"):
        return None

    lines = []
    for i, day in enumerate(daily["time"]):
        wave = _at(daily, "wave_height_max", i)
        period = _at(daily, "wave_period_max", i)
        swell_height = _at(daily, "swell_wave_height_max", i)
        swell_dir = _at(daily, "swell_wave_direction_dominant", i)
        lines.append(
            f"{day}: wave {wave}m max, period {period}s, swell {swell_height}m @ {swell_dir}°"
        )
    return "\n".join(lines)


def _at(daily: dict[str, list[float | None]], key: str, index: int) -> float | None:
    values = daily.get(key)
    return values[index] if values and index < len(values) else None
