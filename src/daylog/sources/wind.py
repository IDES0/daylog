"""Wind forecast via Open-Meteo — free, public, no API key.

Separate host/endpoint from marine.py: Open-Meteo splits wave data
(marine-api.open-meteo.com) from atmospheric data like wind
(api.open-meteo.com). Same no-key, no-auth pattern either way.
"""

from __future__ import annotations

import logging

import httpx

logger = logging.getLogger(__name__)

_URL = "https://api.open-meteo.com/v1/forecast"
_DAILY_FIELDS = "wind_speed_10m_max,wind_gusts_10m_max,wind_direction_10m_dominant"


def fetch_forecast(lat: float, lon: float, timezone: str, days: int = 5) -> str | None:
    """Fetch a daily wind forecast, formatted as plain text for a prompt.

    Returns None on any request failure or missing data — callers should
    treat that as "no forecast available," not raise. A brief is still
    useful without wind data.
    """
    try:
        response = httpx.get(
            _URL,
            params={
                "latitude": lat,
                "longitude": lon,
                "daily": _DAILY_FIELDS,
                "wind_speed_unit": "kn",
                "timezone": timezone,
                "forecast_days": days,
            },
            timeout=10.0,
        )
        response.raise_for_status()
        data = response.json()
    except httpx.HTTPError:
        logger.exception("wind forecast request failed for lat=%s lon=%s", lat, lon)
        return None

    daily = data.get("daily")
    if not daily or not daily.get("time"):
        return None

    lines = []
    for i, day in enumerate(daily["time"]):
        speed = _at(daily, "wind_speed_10m_max", i)
        gusts = _at(daily, "wind_gusts_10m_max", i)
        direction = _at(daily, "wind_direction_10m_dominant", i)
        lines.append(f"{day}: wind {speed}kn (gusts {gusts}kn) @ {direction}°")
    return "\n".join(lines)


def _at(daily: dict[str, list[float | None]], key: str, index: int) -> float | None:
    values = daily.get(key)
    return values[index] if values and index < len(values) else None
