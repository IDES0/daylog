from __future__ import annotations

from zoneinfo import ZoneInfo

import pytest

from daylog import bot
from daylog.sources import marine, wind

_PLACES = [
    {
        "name": "Lombok, Indonesia",
        "activities": ["surf", "diving"],
        "surf_spots": [
            {"name": "Ekas Bay", "lat": -8.9, "lon": 116.45, "break_type": "right/left"},
        ],
        "wind_spots": [
            {"name": "Kuta (wind foiling)", "lat": -8.8948, "lon": 116.2832},
        ],
    },
    {"name": "Flores, Indonesia", "activities": ["diving"]},
]


def test_matching_place_matches_by_substring() -> None:
    place = bot._matching_place(_PLACES, "Kuta, Lombok, ID")
    assert place is not None
    assert place["name"] == "Lombok, Indonesia"


def test_matching_place_no_match_returns_none() -> None:
    assert bot._matching_place(_PLACES, "Uluwatu, Bali, ID") is None


def test_fetch_marine_forecast_combines_current_and_curated_surf_spots(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def _fake_forecast(lat: float, lon: float, timezone: str, days: int = 5) -> str | None:
        return f"forecast for {lat},{lon}"

    monkeypatch.setattr(marine, "fetch_forecast", _fake_forecast)

    current = {"place": "Kuta, Lombok, ID", "lat": -8.8948, "lon": 116.2832}
    result = bot._fetch_marine_forecast(current, _PLACES, ZoneInfo("Asia/Makassar"))

    assert result is not None
    assert "Kuta, Lombok, ID:\nforecast for -8.8948,116.2832" in result
    assert "Ekas Bay:\nforecast for -8.9,116.45" in result


def test_fetch_marine_forecast_none_without_coordinates() -> None:
    assert bot._fetch_marine_forecast(None, _PLACES, ZoneInfo("UTC")) is None
    assert bot._fetch_marine_forecast({"place": "x"}, _PLACES, ZoneInfo("UTC")) is None


def test_fetch_wind_forecast_combines_current_and_curated_wind_spots(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def _fake_forecast(lat: float, lon: float, timezone: str, days: int = 5) -> str | None:
        return f"wind for {lat},{lon}"

    monkeypatch.setattr(wind, "fetch_forecast", _fake_forecast)

    current = {"place": "Kuta, Lombok, ID", "lat": -8.8948, "lon": 116.2832}
    result = bot._fetch_wind_forecast(current, _PLACES, ZoneInfo("Asia/Makassar"))

    assert result is not None
    assert "Kuta, Lombok, ID:\nwind for -8.8948,116.2832" in result
    assert "Ekas Bay:\nwind for -8.9,116.45" in result
    assert "Kuta (wind foiling):\nwind for -8.8948,116.2832" in result


def test_fetch_wind_forecast_none_without_coordinates() -> None:
    assert bot._fetch_wind_forecast(None, _PLACES, ZoneInfo("UTC")) is None
