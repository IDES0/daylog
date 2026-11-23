"""Health trends, baselines, correlations and the assistant's measured text (invented data)."""

from __future__ import annotations

import json
from datetime import date, timedelta
from typing import Any

from daylog import health, health_stats, review
from daylog.vault import Vault

TODAY = date(2026, 11, 23)


def _night(d: date, bed: str = "23:30", wake: str = "07:00", asleep: float = 7.0) -> dict[str, Any]:
    prev = d - timedelta(days=1)
    return {
        "start": f"{prev.isoformat()} {bed}:00 -0600",
        "end": f"{d.isoformat()} {wake}:00 -0600",
        "asleep_h": asleep,
    }


def test_day_numbers_reads_aliases_sleep_and_screen() -> None:
    d = date(2026, 11, 20)
    records = {
        "sleep": [
            _night(d),
            {"start": "2026-11-20 14:00:00", "end": "2026-11-20 14:20:00", "asleep_h": 0.3},
        ],
        "workouts": [{"kind": "Surfing", "minutes": 90}, {"kind": "Walking", "minutes": 30}],
        "screen": [
            {
                "device": "mac",
                "app": "Code",
                "start": "2026-11-20 09:00:00",
                "end": "2026-11-20 11:00:00",
            }
        ],
    }
    out = health_stats.day_numbers({"steps": 9000, "resting_heart_rate": 50}, records)
    assert out["steps"] == 9000 and out["resting_hr"] == 50
    assert (out["bed"], out["wake"], out["bed_h"], out["wake_h"]) == ("23:30", "07:00", -0.5, 7.0)
    assert out["sleep_h"] == 7.0  # the main night, not the nap
    assert (out["workouts"], out["workout_min"]) == (2, 120)
    assert out["screen_mac_min"] == out["screen_min"] == 120


def test_baselines_compare_the_last_week_with_the_four_before() -> None:
    rows = [{"date": f"d{i}", "sleep_h": 8.0} for i in range(28)]
    rows += [{"date": f"r{i}", "sleep_h": 6.5} for i in range(7)]
    b = health_stats.baselines(rows)["sleep_h"]
    assert b == {"recent": 6.5, "baseline": 8.0, "change": -1.5, "days": 7}
    assert health_stats.baselines(rows[-3:])["sleep_h"]["baseline"] is None


def test_correlations_need_enough_days_and_can_look_a_day_ahead() -> None:
    start = date(2026, 10, 1)
    rows: list[dict[str, Any]] = []
    for i in range(14):
        sleep = 5 + (i % 5)
        rows.append(
            {
                "date": (start + timedelta(days=i)).isoformat(),
                "sleep_h": sleep,
                "energy": sleep - 3,
                "screen_min": 100 * (i % 3),
            }
        )
    # Screen time on day d against the sleep filed under d+1.
    for i, row in enumerate(rows[:-1]):
        rows[i + 1]["sleep_h"] = 9 - row["screen_min"] / 100
        rows[i + 1]["energy"] = rows[i + 1]["sleep_h"] - 3
    found = {(c["x"], c["y"]): c for c in health_stats.correlations(rows)}
    assert found[("sleep_h", "energy")]["r"] == 1.0
    assert found[("sleep_h", "energy")]["word"] == "strong positive"
    ahead = found[("screen_min", "sleep_h")]
    assert ahead["next_day"] and ahead["r"] == -1.0 and ahead["n"] == 13
    assert health_stats.correlations(rows[:5]) == []
    assert health_stats.strength(0.1) == "no clear"


def test_brief_text_says_last_night_heart_and_yesterday() -> None:
    rows = []
    records = {}
    for i in range(36):
        d = TODAY - timedelta(days=35 - i)
        rows.append({"date": d.isoformat(), "sleep_h": 7.5, "resting_hr": 50, "hrv": 70})
    rows[-1] = {
        "date": TODAY.isoformat(),
        "sleep_h": 5.9,
        "bed": "01:10",
        "wake": "07:05",
        "sleep_deep_h": 0.8,
        "resting_hr": 56,
        "hrv": 48,
    }
    rows[-2]["steps"] = 12034
    records[TODAY - timedelta(days=1)] = {
        "workouts": [{"kind": "Surfing", "minutes": 95}],
        "screen": [
            {
                "device": "mac",
                "app": "Dia",
                "site": "youtube.com",
                "start": "2026-11-22 21:00:00",
                "end": "2026-11-22 23:30:00",
            }
        ],
    }
    text = health_stats.brief_text(TODAY, rows, records)
    assert (
        "Last night: slept 5.9 h (01:10–07:05); deep 0.8; 7-day avg 7.5 h (28-day 7.5 h)." in text
    )
    assert "Heart: resting HR 56 bpm (28-day 50 bpm); HRV 48 ms (28-day 70 ms)." in text
    assert (
        "Yesterday: 1 workout (Surfing 1 h 35); 12,034 steps; "
        "mac screen 2 h 30 (youtube.com 2 h 30)." in text
    )
    assert health_stats.brief_text(TODAY, [{"date": TODAY.isoformat()}], {}) == ""


def test_review_text_and_the_review_input(vault: Vault) -> None:
    body = {
        "data": {
            "metrics": [
                {
                    "name": "step_count",
                    "units": "count",
                    "data": [{"date": "2026-11-17 00:00:00 -0600", "qty": 10000}],
                }
            ],
            "workouts": [
                {
                    "id": "W1",
                    "name": "Surfing",
                    "start": "2026-11-18 07:00:00 -0600",
                    "end": "2026-11-18 08:30:00 -0600",
                    "duration": 5400,
                }
            ],
        }
    }
    health.ingest(vault, json.dumps(body).encode(), TODAY)
    text = health_stats.review_section(vault, date(2026, 11, 17), date(2026, 11, 23))
    assert "- Steps: avg 10000.0 over 1 days" in text
    assert "- Surfing: 1× · 1 h 30" in text
    built = review.build_input({}, {}, "(none)", None, "$0", measured=text)
    assert "Measured by their devices this week (exact):\n- Steps" in built
    assert "Measured by" not in review.build_input({}, {}, "(none)", None, "$0")


def test_chat_get_health_tool(vault: Vault) -> None:
    from daylog import chat

    body = {
        "data": {
            "workouts": [
                {
                    "id": "W1",
                    "name": "Surfing",
                    "start": "2026-11-21 07:00:00 -0600",
                    "end": "2026-11-21 08:30:00 -0600",
                    "duration": 5400,
                }
            ]
        },
        "screen_totals": {"date": "2026-11-21", "apps": {"Instagram": "1h 10m"}},
    }
    health.ingest(vault, json.dumps(body).encode(), TODAY)
    tools = chat.Tools(vault, TODAY)
    out = tools.t_get_health("2026-11-20", "2026-11-22")
    assert "## 2026-11-21" in out and "Surfing" in out and "Instagram" in out
    assert "Nothing measured" in tools.t_get_health("2026-01-01", "2026-01-05")
