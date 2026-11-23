"""Health Auto Export payloads and screen time through the inlet (invented data)."""

from __future__ import annotations

import json
from datetime import date
from typing import Any

import pytest

from daylog import health
from daylog.vault import Vault

TODAY = date(2026, 11, 20)


def _hae(metrics: list[Any] | None = None, workouts: list[Any] | None = None) -> dict[str, Any]:
    return {"data": {"metrics": metrics or [], "workouts": workouts or []}}


STEPS = {
    "name": "step_count",
    "units": "count",
    "data": [
        {"date": "2026-11-19 00:00:00 -0600", "qty": 8423, "source": "Watch"},
        {"date": "2026-11-20 00:00:00 -0600", "qty": 4000},
        {"date": "2026-11-20 12:00:00 -0600", "qty": 1000},
    ],
}
HR = {
    "name": "heart_rate",
    "units": "count/min",
    "data": [
        {"date": "2026-11-20 00:00:00 -0600", "Min": 48, "Avg": 70, "Max": 150},
        {"date": "2026-11-20 12:00:00 -0600", "Min": 52, "Avg": 80, "Max": 171},
    ],
}
RHR = {
    "name": "resting_heart_rate",
    "units": "count/min",
    "data": [{"date": "2026-11-20 00:00:00 -0600", "qty": 51}],
}
SLEEP_SUMMARY = {
    "name": "sleep_analysis",
    "units": "hr",
    "data": [
        {
            "date": "2026-11-20 00:00:00 -0600",
            "totalSleep": 7.1,
            "asleep": 0,
            "core": 4.4,
            "deep": 1.1,
            "rem": 1.6,
            "awake": 0.4,
            "inBed": 7.6,
            "sleepStart": "2026-11-19 23:12:00 -0600",
            "sleepEnd": "2026-11-20 06:48:00 -0600",
            "inBedStart": "2026-11-19 23:00:00 -0600",
            "inBedEnd": "2026-11-20 06:55:00 -0600",
            "source": "Alex's Apple Watch",
        }
    ],
}
SURF_V2 = {
    "id": "W-1",
    "name": "Surfing",
    "start": "2026-11-20 07:10:00 -0600",
    "end": "2026-11-20 09:00:00 -0600",
    "duration": 6600,
    "location": "Outdoor",
    "activeEnergyBurned": {"qty": 540.4, "units": "kcal"},
    "activeEnergy": [{"date": "2026-11-20 07:11:00 -0600", "qty": 5, "units": "kcal"}],
    "distance": {"qty": 2, "units": "mi"},
    "heartRate": {
        "min": {"qty": 80, "units": "bpm"},
        "avg": {"qty": 128.4, "units": "bpm"},
        "max": {"qty": 171, "units": "bpm"},
    },
    "elevationUp": {"qty": 10, "units": "ft"},
    "source": {"name": "Apple Watch", "identifier": "com.apple.health"},
    "route": [{"latitude": 1, "longitude": 2}],
}
RUN_V1 = {
    "name": "Running",
    "start": "2026-11-20 17:00:00 -0600",
    "end": "2026-11-20 17:30:00 -0600",
    "activeEnergy": {"qty": 1255, "units": "kJ"},
    "distance": {"qty": 5, "units": "km"},
    "heartRateData": [
        {"date": "2026-11-20 17:01:00 -0600", "qty": 140, "units": "bpm"},
        {"date": "2026-11-20 17:20:00 -0600", "qty": 160, "units": "bpm"},
    ],
}


def test_stamp_normalises_device_times() -> None:
    assert health.stamp("2026-11-20 06:12:00 -0600") == "2026-11-20 06:12:00 -0600"
    assert health.stamp("2026-11-20T06:12:00-06:00") == "2026-11-20 06:12:00 -0600"
    assert health.stamp("2026-11-20T06:12:05.123Z") == "2026-11-20 06:12:05 +0000"
    assert health.stamp("2026-11-20 06:12") == "2026-11-20 06:12:00"
    for bad in ("2026-13-01 00:00", "yesterday", None, "2026-11-20 25:00"):
        assert health.stamp(bad) is None


def test_metrics_become_daily_numbers() -> None:
    batch = health.parse_export(_hae([STEPS, HR, RHR]))
    assert batch.daily[date(2026, 11, 19)] == {"step_count": 8423.0}
    today = batch.daily[TODAY]
    assert today["step_count"] == 5000.0  # hourly entries add up
    assert today["resting_heart_rate"] == 51.0
    assert (today["heart_rate_min"], today["heart_rate_avg"], today["heart_rate_max"]) == (
        48.0,
        75.0,
        171.0,
    )
    assert batch.units == {
        "step_count": "count",
        "heart_rate": "count/min",
        "resting_heart_rate": "count/min",
    }


def test_summarised_sleep_files_under_the_morning() -> None:
    batch = health.parse_export(_hae([SLEEP_SUMMARY]))
    (night,) = batch.days[TODAY]["sleep"]
    assert night["start"] == "2026-11-19 23:12:00 -0600"
    assert night["end"] == "2026-11-20 06:48:00 -0600"
    assert night["asleep_h"] == 7.1
    assert (night["deep_h"], night["rem_h"], night["core_h"]) == (1.1, 1.6, 4.4)
    assert batch.daily[TODAY]["sleep_h"] == 7.1
    assert batch.daily[TODAY]["in_bed_h"] == 7.6


def test_sleep_segments_group_into_nights_and_prefer_the_watch() -> None:
    def seg(start: str, end: str, value: str, source: str = "Watch") -> dict[str, Any]:
        return {"startDate": start, "endDate": end, "value": value, "qty": 0, "source": source}

    batch = health.parse_export(
        _hae(
            [
                {
                    "name": "sleep_analysis",
                    "units": "hr",
                    "data": [
                        seg(
                            "2026-11-19 22:50:00 -0600",
                            "2026-11-20 07:00:00 -0600",
                            "In Bed",
                            "Phone",
                        ),
                        seg(
                            "2026-11-19 23:00:00 -0600",
                            "2026-11-20 06:30:00 -0600",
                            "Asleep",
                            "Phone",
                        ),
                        seg("2026-11-19 23:10:00 -0600", "2026-11-20 01:10:00 -0600", "Core"),
                        seg("2026-11-20 01:10:00 -0600", "2026-11-20 02:10:00 -0600", "Deep"),
                        seg("2026-11-20 02:10:00 -0600", "2026-11-20 02:40:00 -0600", "Awake"),
                        seg("2026-11-20 02:40:00 -0600", "2026-11-20 06:40:00 -0600", "REM"),
                        # an afternoon nap, a separate record
                        seg("2026-11-20 14:00:00 -0600", "2026-11-20 14:30:00 -0600", "Core"),
                    ],
                }
            ]
        )
    )
    night, nap = batch.days[TODAY]["sleep"]
    assert night["start"] == "2026-11-19 23:10:00 -0600"
    assert night["end"] == "2026-11-20 06:40:00 -0600"
    assert night["asleep_h"] == 7.0  # the phone's overlapping "Asleep" is dropped
    assert night["awake_h"] == 0.5
    assert night["source"] == "Watch"
    assert nap["asleep_h"] == 0.5
    assert batch.daily[TODAY]["sleep_h"] == 7.5


def test_workouts_v1_and_v2() -> None:
    batch = health.parse_export(_hae(workouts=[SURF_V2, RUN_V1]))
    surf, run = batch.days[TODAY]["workouts"]
    assert surf == {
        "id": "W-1",
        "kind": "Surfing",
        "start": "2026-11-20 07:10:00 -0600",
        "end": "2026-11-20 09:00:00 -0600",
        "minutes": 110.0,
        "kcal": 540.0,
        "distance_km": 3.22,
        "hr_avg": 128.0,
        "hr_max": 171.0,
        "elevation_up_m": 3.0,
        "location": "Outdoor",
        "source": "Apple Watch",
    }
    assert run["id"] == "Running@2026-11-20 17:00:00 -0600"
    assert run["minutes"] == 30.0
    assert run["kcal"] == 300.0  # kJ converted
    assert (run["hr_avg"], run["hr_max"]) == (150.0, 160.0)


def test_screen_sessions_split_at_midnight_and_events_kept() -> None:
    batch = health.parse_export(
        {
            "screen": [
                {
                    "device": "mac",
                    "app": "Dia",
                    "site": "YouTube.com",
                    "start": "2026-11-19 23:30:00 -0600",
                    "end": "2026-11-20 00:45:00 -0600",
                },
                {"app": "Code", "start": "2026-11-20 10:00:00", "end": "2026-11-20 09:00:00"},
            ],
            "screen_event": {
                "app": "Instagram",
                "event": "Opened",
                "at": "2026-11-20T22:10:00-06:00",
            },
        }
    )
    (late,) = batch.days[date(2026, 11, 19)]["screen"]
    (early,) = batch.days[TODAY]["screen"]
    assert (late["start"], late["end"]) == (
        "2026-11-19 23:30:00 -0600",
        "2026-11-19 23:59:59 -0600",
    )
    assert (early["start"], early["site"]) == ("2026-11-20 00:00:00 -0600", "youtube.com")
    assert batch.days[TODAY]["screen_events"] == [
        {"device": "iphone", "app": "Instagram", "event": "open", "at": "2026-11-20 22:10:00 -0600"}
    ]
    assert batch.ignored == ["screen Code"]


def test_screen_totals_take_several_shapes() -> None:
    assert health.minutes("1h 23m") == 83
    assert health.minutes("1 hr 5 min") == 65
    assert health.minutes("1:30") == 90
    assert health.minutes("45") == 45
    assert health.minutes("90s") == 1.5
    batch = health.parse_export(
        {
            "screen_totals": {
                "date": "2026-11-20",
                "apps": "Instagram: 1h 23m\nYouTube - 45 min\nSafari: 0:12\nnonsense",
            }
        }
    )
    assert batch.days[TODAY]["screen_totals"] == [
        {"device": "iphone", "app": "Instagram", "minutes": 83.0},
        {"device": "iphone", "app": "YouTube", "minutes": 45.0},
        {"device": "iphone", "app": "Safari", "minutes": 12.0},
    ]
    listed = health.parse_export(
        {"screen_totals": [{"date": "2026-11-20", "apps": [{"app": "Maps", "minutes": 9}]}]}
    )
    assert listed.days[TODAY]["screen_totals"][0]["minutes"] == 9.0


def test_merge_records_is_idempotent_and_replaces_what_grew() -> None:
    first = health.parse_export(_hae([SLEEP_SUMMARY], [SURF_V2])).days[TODAY]
    merged = health.merge_records({}, first)
    assert health.merge_records(merged, first) == merged
    longer = dict(SURF_V2, end="2026-11-20 09:30:00 -0600", duration=8400)
    again = health.merge_records(merged, health.parse_export(_hae(workouts=[longer])).days[TODAY])
    assert [w["minutes"] for w in again["workouts"]] == [140.0]
    s1 = {
        "device": "mac",
        "app": "Code",
        "start": "2026-11-20 10:00:00",
        "end": "2026-11-20 10:05:00",
    }
    grown = dict(s1, end="2026-11-20 10:40:00")
    screens = health.merge_records(health.merge_records({}, {"screen": [s1]}), {"screen": [grown]})
    assert screens["screen"] == [grown]
    totals = health.merge_records(
        {"screen_totals": [{"device": "iphone", "app": "Old", "minutes": 5}]},
        {"screen_totals": [{"device": "iphone", "app": "New", "minutes": 7}]},
    )
    assert totals["screen_totals"] == [{"device": "iphone", "app": "New", "minutes": 7}]


def test_ingest_writes_one_commit_and_reports(vault: Vault) -> None:
    body = json.dumps(_hae([STEPS, SLEEP_SUMMARY], [SURF_V2])).encode()
    reply = health.ingest(vault, body, TODAY)
    assert reply.startswith(
        "saved 2 days of numbers, 1 night of sleep, 1 workout for 2026-11-19..2026-11-20"
    )
    assert vault.read_yaml(health.FILE, {})["2026-11-20"]["sleep_h"] == 7.1
    days = health.read_days(vault, date(2026, 11, 18), TODAY)
    assert list(days) == [TODAY]
    assert days[TODAY]["workouts"][0]["kind"] == "Surfing"
    assert vault.read_yaml(health.UNITS_FILE, {}) == {"step_count": "count"}
    log = [subject for _, subject in vault.recent_changes(5)]
    assert sum("health:" in line for line in log) == 1
    assert "(nothing new)" in health.ingest(vault, body, TODAY)


def test_ingest_still_takes_the_simple_shape_and_refuses_junk(vault: Vault) -> None:
    assert health.ingest(vault, b'{"date": "2026-11-20", "steps": 9}', TODAY).startswith(
        "saved steps for 2026-11-20"
    )
    assert health.ingest(vault, b'{"data": {"metrics": [], "workouts": []}}', TODAY) == (
        "nothing new to save"
    )
    assert health.ingest(vault, b'{"data": {"stateOfMind": [{"x": 1}]}}', TODAY) == (
        "nothing new to save"
    )
    with pytest.raises(health.HealthError):
        health.ingest(vault, b"nope", TODAY)


def test_the_inlet_answers_a_url_check(vault: Vault, monkeypatch: pytest.MonkeyPatch) -> None:
    import http.client
    import threading
    from http.server import ThreadingHTTPServer

    from daylog import calendar_server

    monkeypatch.setenv("VAULT_PATH", str(vault.path))
    handler = calendar_server._make_handler(None, "/health/s3cret")
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    try:
        for method, path, code in (
            ("GET", "/health/s3cret", 200),
            ("HEAD", "/health/s3cret", 200),
            ("GET", "/health/wrong", 404),
            ("HEAD", "/health/wrong", 404),
        ):
            conn = http.client.HTTPConnection("127.0.0.1", httpd.server_address[1], timeout=5)
            conn.request(method, path)
            assert conn.getresponse().status == code, (method, path)
    finally:
        httpd.shutdown()


def test_zero_in_bed_is_unknown_and_long_metric_names_are_kept() -> None:
    first: dict[str, Any] = SLEEP_SUMMARY["data"][0]  # type: ignore[assignment]
    night = {**first, "inBed": 0}
    metric = {
        "name": "walking_double_support_percentage",
        "units": "%",
        "data": [{"date": "2026-11-20 00:00:00 -0600", "qty": 27.5}],
    }
    batch = health.parse_export(_hae([dict(SLEEP_SUMMARY, data=[night]), metric]))
    assert "in_bed_h" not in batch.days[TODAY]["sleep"][0]
    assert "in_bed_h" not in batch.daily[TODAY]
    assert batch.daily[TODAY]["walking_double_support_percentage"] == 27.5


def test_reply_counts_read_naturally() -> None:
    assert health._count(1, "night of sleep") == "1 night of sleep"
    assert health._count(3, "night of sleep") == "3 nights of sleep"
    assert health._count(5, "screen session") == "5 screen sessions"
    assert health._count(2, "workout") == "2 workouts"
