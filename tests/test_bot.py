from __future__ import annotations

from datetime import date
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


_TODAY = date(2026, 8, 21)
_EXISTING_FRONTMATTER = {
    "activities": [{"type": "surf", "hours": 2.0, "detail": "Echo Beach"}],
    "skipped": ["gym"],
}


def test_resolve_corrections_valid_reference() -> None:
    resolved = bot._resolve_corrections(
        _EXISTING_FRONTMATTER,
        [{"field": "skipped", "index": 0, "reason": "went after all"}],
        _TODAY,
    )

    assert len(resolved) == 1
    assert resolved[0].field == "skipped"
    assert resolved[0].item == "gym"
    assert resolved[0].description == "gym"
    assert resolved[0].reason == "went after all"
    assert resolved[0].entry_date == _TODAY


def test_resolve_corrections_describes_activity() -> None:
    resolved = bot._resolve_corrections(
        _EXISTING_FRONTMATTER, [{"field": "activities", "index": 0}], _TODAY
    )

    assert len(resolved) == 1
    assert resolved[0].description == "surf, 2h — Echo Beach"


def test_resolve_corrections_out_of_range_dropped() -> None:
    resolved = bot._resolve_corrections(
        _EXISTING_FRONTMATTER, [{"field": "skipped", "index": 5}], _TODAY
    )
    assert resolved == []


def test_resolve_corrections_unsupported_field_dropped() -> None:
    resolved = bot._resolve_corrections(
        _EXISTING_FRONTMATTER, [{"field": "goal_progress", "index": 0}], _TODAY
    )
    assert resolved == []


def test_resolve_corrections_no_existing_entry_dropped() -> None:
    resolved = bot._resolve_corrections(None, [{"field": "skipped", "index": 0}], _TODAY)
    assert resolved == []


def test_upcoming_items_sorted_earliest_first() -> None:
    goals_data = [
        {"id": "jobs", "title": "Jobs", "type": "hard", "deadline": date(2026, 10, 31)},
        {
            "id": "surf",
            "title": "Surf",
            "type": "soft",
            "target_window": [date(2026, 9, 1), date(2026, 9, 15)],
        },
    ]
    itinerary_data = [
        {"id": "visa", "place": "Indonesia exit", "type": "hard", "deadline": date(2026, 9, 20)},
    ]

    lines = bot._upcoming_items(goals_data, itinerary_data, today=date(2026, 9, 1))

    assert lines == [
        "2026-09-15: Surf — target",
        "2026-09-20: Indonesia exit — deadline",
        "2026-10-31: Jobs — deadline",
    ]


def test_upcoming_items_flags_overdue() -> None:
    goals_data = [{"id": "jobs", "title": "Jobs", "type": "hard", "deadline": date(2026, 8, 1)}]

    lines = bot._upcoming_items(goals_data, [], today=date(2026, 9, 1))

    assert lines == ["2026-08-01: Jobs — deadline (overdue)"]


def test_upcoming_items_skips_done_and_dropped() -> None:
    goals_data = [
        {"id": "a", "title": "A", "type": "hard", "deadline": date(2026, 9, 1), "status": "done"},
        {
            "id": "b",
            "title": "B",
            "type": "hard",
            "deadline": date(2026, 9, 2),
            "status": "dropped",
        },
    ]

    assert bot._upcoming_items(goals_data, [], today=date(2026, 9, 1)) == []


def test_upcoming_items_skips_undated_entries() -> None:
    goals_data = [{"id": "a", "title": "A", "type": "soft", "status": "active"}]
    assert bot._upcoming_items(goals_data, [], today=date(2026, 9, 1)) == []


def test_format_short_date() -> None:
    assert bot._format_short_date(date(2026, 9, 8)) == "Sep 8"


def test_backdate_options_labels_today_and_yesterday_by_name() -> None:
    options = bot._backdate_options(date(2026, 9, 9), days=3)

    assert options[0] == (date(2026, 9, 9), "Today (Sep 9)")
    assert options[1] == (date(2026, 9, 8), "Yesterday (Sep 8)")
    assert options[2] == (date(2026, 9, 7), "Monday (Sep 7)")


def test_backdate_options_length_matches_days() -> None:
    assert len(bot._backdate_options(date(2026, 9, 9), days=7)) == 7


def test_resolve_other_day_notes_valid() -> None:
    resolved = bot._resolve_other_day_notes(
        [
            {
                "date": "2026-09-08",
                "summary": "Also surfed yesterday.",
                "activities": [{"type": "surf", "hours": 1.0}],
            }
        ]
    )

    assert len(resolved) == 1
    assert resolved[0].date == date(2026, 9, 8)
    assert resolved[0].summary == "Also surfed yesterday."
    assert resolved[0].facts == {"activities": [{"type": "surf", "hours": 1.0}]}


def test_resolve_other_day_notes_drops_missing_summary() -> None:
    assert bot._resolve_other_day_notes([{"date": "2026-09-08"}]) == []


def test_resolve_other_day_notes_drops_missing_date() -> None:
    assert bot._resolve_other_day_notes([{"summary": "x"}]) == []


def test_resolve_other_day_notes_drops_invalid_date() -> None:
    assert bot._resolve_other_day_notes([{"date": "not-a-date", "summary": "x"}]) == []


def test_resolve_other_day_notes_only_keeps_known_fact_fields() -> None:
    resolved = bot._resolve_other_day_notes(
        [{"date": "2026-09-08", "summary": "x", "goal_progress": [{"goal_id": "g", "delta": 1}]}]
    )

    assert resolved[0].facts == {}
