"""GET /api/health and the measured calendar, through the web API (invented data)."""

from __future__ import annotations

import json
from datetime import date, datetime, timedelta
from typing import Any

import pytest

from daylog import health
from daylog.runtime import tz
from daylog.vault import Vault
from daylog.webapp import api as webapi

NOW = datetime(2026, 11, 23, 12, 0, tzinfo=tz())
TODAY = date(2026, 11, 23)
DAY = date(2026, 11, 20)


def _api(vault: Vault) -> webapi.Api:
    return webapi.Api(
        vault=lambda: vault, now=lambda: NOW, spawn=lambda fn: None, run_notes=lambda fn: fn()
    )


def _get(api: webapi.Api, path: str, signed_in: bool = True, **query: str) -> tuple[int, Any]:
    req = webapi.Request(
        method="GET",
        path=path,
        query={k: [v] for k, v in query.items()},
        signed_in=signed_in,
    )
    status, body = api.handle(req)
    return status, body


def _export() -> bytes:
    body = {
        "data": {
            "metrics": [
                {
                    "name": "step_count",
                    "units": "count",
                    "data": [
                        {"date": "2026-11-20 00:00:00 -0600", "qty": 8000},
                        {"date": "2026-11-21 00:00:00 -0600", "qty": 11000},
                    ],
                },
                {
                    "name": "resting_heart_rate",
                    "units": "count/min",
                    "data": [{"date": "2026-11-20 00:00:00 -0600", "qty": 52}],
                },
                {
                    "name": "sleep_analysis",
                    "units": "hr",
                    "data": [
                        {
                            "date": "2026-11-20 00:00:00 -0600",
                            "totalSleep": 7.1,
                            "core": 4.4,
                            "deep": 1.1,
                            "rem": 1.6,
                            "sleepStart": "2026-11-19 23:12:00 -0600",
                            "sleepEnd": "2026-11-20 06:48:00 -0600",
                        }
                    ],
                },
            ],
            "workouts": [
                {
                    "id": "W1",
                    "name": "Surfing",
                    "start": "2026-11-20 07:10:00 -0600",
                    "end": "2026-11-20 09:00:00 -0600",
                    "duration": 6600,
                },
                {
                    "id": "W2",
                    "name": "Surfing",
                    "start": "2026-11-21 08:00:00 -0600",
                    "end": "2026-11-21 09:00:00 -0600",
                    "duration": 3600,
                },
                {
                    "id": "W3",
                    "name": "Walking",
                    "start": "2026-11-21 18:00:00 -0600",
                    "end": "2026-11-21 18:30:00 -0600",
                    "duration": 1800,
                },
            ],
        }
    }
    return json.dumps(body).encode()


@pytest.fixture
def seeded(vault: Vault) -> Vault:
    health.ingest(vault, _export(), TODAY)
    vault.write_journal_entry(
        datetime(2026, 11, 20, 20, 0),
        {"felt": [{"energy": 4, "mood": 3}]},
        "Felt good after the surf.",
        "Good day.",
    )
    return vault


def test_signed_out_gets_401(vault: Vault) -> None:
    status, body = _get(_api(vault), "/api/health", signed_in=False)
    assert status == 401
    assert body == {"error": "signed_out"}


def test_empty_vault_has_a_full_series_and_nothing_measured(vault: Vault) -> None:
    status, body = _get(_api(vault), "/api/health")
    assert status == 200
    assert body["days"] == 30
    assert len(body["series"]) == 30
    assert body["last_measured"] is None
    assert body["series"][-1] == {"date": TODAY.isoformat()}
    assert body["workouts"] == [] and body["screen"] == {}


def test_measured_fields_land_on_their_dates(seeded: Vault) -> None:
    status, body = _get(_api(seeded), "/api/health", days="14")
    assert status == 200
    assert body["today"] == TODAY.isoformat()
    assert len(body["series"]) == body["days"] == 14
    dates = [r["date"] for r in body["series"]]
    assert dates[0] == (TODAY - timedelta(days=13)).isoformat()
    assert dates[-1] == TODAY.isoformat()
    rows = {r["date"]: r for r in body["series"]}

    fri = rows["2026-11-20"]
    assert fri["steps"] == 8000
    assert fri["resting_hr"] == 52
    assert fri["sleep_h"] == pytest.approx(7.1)
    assert fri["sleep_deep_h"] == pytest.approx(1.1)
    assert fri["sleep_rem_h"] == pytest.approx(1.6)
    assert (fri["bed"], fri["wake"]) == ("23:12", "06:48")
    assert fri["bed_h"] == pytest.approx(-0.8)
    assert fri["wake_h"] == pytest.approx(6.8)
    assert (fri["workouts"], fri["workout_min"]) == (1, 110)
    assert (fri["energy"], fri["mood"]) == (4, 3)

    sat = rows["2026-11-21"]
    assert sat["steps"] == 11000
    assert (sat["workouts"], sat["workout_min"]) == (2, 90)
    assert "sleep_h" not in sat and "energy" not in sat
    # Days with nothing measured are present, with no numbers (gaps, not zeros).
    assert rows["2026-11-22"] == {"date": "2026-11-22"}
    assert body["last_measured"] == "2026-11-21"
    assert body["labels"]["sleep_h"] == ["Sleep", "h"]
    assert body["labels"]["bed_h"] == ["Fell asleep", "clock"]
    assert body["baselines"]["steps"]["recent"] == pytest.approx(9500)
    assert body["baselines"]["steps"]["baseline"] is None


def test_workout_totals_per_kind(seeded: Vault) -> None:
    _, body = _get(_api(seeded), "/api/health", days="7")
    assert body["workouts"] == [
        {"kind": "Surfing", "sessions": 2, "minutes": 170, "kcal": 0},
        {"kind": "Walking", "sessions": 1, "minutes": 30, "kcal": 0},
    ]


def test_days_is_clamped_and_checked(seeded: Vault) -> None:
    api = _api(seeded)
    status, body = _get(api, "/api/health", days="2")
    assert status == 200 and body["days"] == 7 and len(body["series"]) == 7
    _, body = _get(api, "/api/health", days="9999")
    assert body["days"] == 400 and len(body["series"]) == 400
    status, body = _get(api, "/api/health", days="lots")
    assert status == 400 and body["error"] == "bad_number"


def test_calendar_week_shows_sleep_and_measured_workout(seeded: Vault) -> None:
    status, body = _get(_api(seeded), "/api/calendar", week=DAY.isoformat())
    assert status == 200
    assert body["week_of"] == "2026-11-16"
    days = {d["date"]: d for d in body["days"]}
    blocks = [b for d in body["days"] for b in d["blocks"]]
    assert any(b["layer"] == "sleep" for b in blocks)
    surf = [b for b in days["2026-11-20"]["blocks"] if b["time"] == "measured"]
    assert any(b["label"] == "surfing" for b in surf)
    measured = days["2026-11-20"]["measured"]
    assert measured["steps"] == 8000
    assert measured["sleep_h"] == pytest.approx(7.1)
    assert (measured["workouts"], measured["workout_min"]) == (1, 110)
    assert days["2026-11-22"]["measured"] == {}
