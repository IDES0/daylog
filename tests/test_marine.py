from __future__ import annotations

from typing import Any

import httpx
import pytest

from daylog.sources import marine

_SAMPLE_RESPONSE = {
    "daily": {
        "time": ["2026-08-21", "2026-08-22"],
        "wave_height_max": [1.78, 1.7],
        "wave_period_max": [12.1, 11.95],
        "swell_wave_height_max": [1.6, 1.4],
        "swell_wave_period_max": [12.1, 12.25],
        "swell_wave_direction_dominant": [212, 211],
    }
}


class _FakeResponse:
    def __init__(self, json_data: dict[str, Any], status_code: int = 200) -> None:
        self._json_data = json_data
        self.status_code = status_code

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise httpx.HTTPStatusError("error", request=None, response=self)  # type: ignore[arg-type]

    def json(self) -> dict[str, Any]:
        return self._json_data


def test_fetch_forecast_formats_daily_data(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(httpx, "get", lambda *a, **kw: _FakeResponse(_SAMPLE_RESPONSE))

    result = marine.fetch_forecast(-8.8948, 116.2832, "Asia/Makassar")

    assert result is not None
    assert "2026-08-21: wave 1.78m max, period 12.1s, swell 1.6m @ 212°" in result
    assert "2026-08-22" in result


def test_fetch_forecast_returns_none_on_http_error(monkeypatch: pytest.MonkeyPatch) -> None:
    def _raise(*args: object, **kwargs: object) -> _FakeResponse:
        raise httpx.ConnectError("no network")

    monkeypatch.setattr(httpx, "get", _raise)

    assert marine.fetch_forecast(-8.8948, 116.2832, "Asia/Makassar") is None


def test_fetch_forecast_returns_none_on_http_status_error(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(httpx, "get", lambda *a, **kw: _FakeResponse({}, status_code=500))

    assert marine.fetch_forecast(-8.8948, 116.2832, "Asia/Makassar") is None


def test_fetch_forecast_returns_none_when_no_daily_data(monkeypatch: pytest.MonkeyPatch) -> None:
    # e.g. an inland location with no marine forecast available
    empty = _FakeResponse({"daily": {"time": []}})
    monkeypatch.setattr(httpx, "get", lambda *a, **kw: empty)

    assert marine.fetch_forecast(-8.8948, 116.2832, "Asia/Makassar") is None
