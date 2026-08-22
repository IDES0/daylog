from __future__ import annotations

from typing import Any

import httpx
import pytest

from daylog.sources import wind

_SAMPLE_RESPONSE = {
    "daily": {
        "time": ["2026-08-22", "2026-08-23"],
        "wind_speed_10m_max": [14.2, 16.8],
        "wind_gusts_10m_max": [19.5, 22.1],
        "wind_direction_10m_dominant": [150, 155],
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

    result = wind.fetch_forecast(-8.8948, 116.2832, "Asia/Makassar")

    assert result is not None
    assert "2026-08-22: wind 14.2kn (gusts 19.5kn) @ 150°" in result
    assert "2026-08-23" in result


def test_fetch_forecast_returns_none_on_http_error(monkeypatch: pytest.MonkeyPatch) -> None:
    def _raise(*args: object, **kwargs: object) -> _FakeResponse:
        raise httpx.ConnectError("no network")

    monkeypatch.setattr(httpx, "get", _raise)

    assert wind.fetch_forecast(-8.8948, 116.2832, "Asia/Makassar") is None


def test_fetch_forecast_returns_none_on_http_status_error(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(httpx, "get", lambda *a, **kw: _FakeResponse({}, status_code=500))

    assert wind.fetch_forecast(-8.8948, 116.2832, "Asia/Makassar") is None


def test_fetch_forecast_returns_none_when_no_daily_data(monkeypatch: pytest.MonkeyPatch) -> None:
    empty = _FakeResponse({"daily": {"time": []}})
    monkeypatch.setattr(httpx, "get", lambda *a, **kw: empty)

    assert wind.fetch_forecast(-8.8948, 116.2832, "Asia/Makassar") is None
