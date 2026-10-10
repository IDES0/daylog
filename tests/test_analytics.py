from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import httpx
import pytest

from daylog import analytics

NOW = datetime(2026, 10, 10, 12, 0, tzinfo=UTC)


def _row(person: str = "p1", ref: str = "acme", last: str = "2026-10-10T11:50:00Z") -> dict:
    return {
        "person": person,
        "ref": ref,
        "first_seen": "2026-10-10T11:40:00.150000Z",
        "last_seen": last,
        "pages": ["/resume", "/"],
        "resume": 1,
        "chess": 0,
        "spacetime": 3,
        "city": "Austin",
        "country": "US",
        "session": "s-123",
    }


def test_new_visits_alert_once_per_person_and_ref() -> None:
    fresh, state = analytics.new_visits({}, [_row(), _row(ref="globex")], NOW)
    assert [v.ref for v in fresh] == ["acme", "globex"]
    assert state["cursor"] == "2026-10-10T11:50:00+00:00"

    again, later = analytics.new_visits(state, [_row(last="2026-10-10T12:05:00Z")], NOW)
    assert again == []
    assert later["cursor"] == "2026-10-10T12:05:00+00:00"


def test_vault_state_holds_summaries_not_identities() -> None:
    _, state = analytics.new_visits({}, [_row()], NOW)
    text = repr(state)
    assert "p1" not in text and "Austin" not in text and "s-123" not in text
    assert state["company_visits"] == [
        {
            "date": "2026-10-10",
            "ref": "acme",
            "pages": 2,
            "resume": True,
            "chess": False,
            "spacetime": True,
            "country": "US",
        }
    ]


def test_poll_starts_a_day_back_then_overlaps_the_cursor() -> None:
    assert analytics.poll_since({}, NOW) == NOW - analytics.FIRST_LOOKBACK
    since = analytics.poll_since({"cursor": "2026-10-10T11:50:00+00:00"}, NOW)
    assert since == datetime(2026, 10, 10, 11, 40, tzinfo=UTC)


def test_visits_query_excludes_channels_and_tests() -> None:
    q = analytics.visits_hogql(NOW)
    assert "'linkedin', 'github', 'test', 'claude-test'" in q
    assert "toDateTime('2026-10-10 12:00:00', 'UTC')" in q


def test_alert_has_ref_pages_actions_place_and_recording(monkeypatch: Any) -> None:
    monkeypatch.setenv("POSTHOG_PROJECT_ID", "123")
    fresh, _ = analytics.new_visits({}, [_row()], NOW)
    text = analytics.format_alert(fresh[0])
    assert "ref=acme" in text and "/, /resume" in text
    assert "downloaded the resume; tried spacetime" in text
    assert "Austin, US" in text
    assert "https://us.posthog.com/project/123/replay/s-123" in text


def test_summary_section_and_company_refs() -> None:
    summary = analytics.summarize(
        [{"people": 5, "views": 12, "resume": 1, "chess": 2, "spacetime": 0}],
        [
            {"ref": "", "people": 3, "views": 6},
            {"ref": "linkedin", "people": 1, "views": 2},
            {"ref": "acme", "people": 1, "views": 4},
        ],
    )
    assert summary["company_refs"] == ["acme"]
    text = analytics.format_section(summary)
    assert "Visitors: 5 (12 page views)" in text
    assert "By ref: (none) 3 · linkedin 1 · acme 1" in text
    assert "Companies: acme" in text


def test_record_day_keeps_a_bounded_history() -> None:
    state: dict = {"days": {f"2026-01-{d:02d}": {} for d in range(1, 32)}}
    for month in range(2, 7):
        for d in range(1, 29):
            state = analytics.record_day(state, f"2026-{month:02d}-{d:02d}", {"people": 1})
    assert len(state["days"]) == analytics.KEEP_DAYS
    assert max(state["days"]) == "2026-06-28"


def test_query_sends_bearer_key_and_maps_columns(monkeypatch: Any) -> None:
    monkeypatch.setenv("POSTHOG_PERSONAL_API_KEY", "phx_test")
    monkeypatch.setenv("POSTHOG_PROJECT_ID", "123")
    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["auth"] = request.headers["Authorization"]
        seen["url"] = str(request.url)
        return httpx.Response(200, json={"columns": ["a", "b"], "results": [[1, 2]]})

    client = httpx.Client(transport=httpx.MockTransport(handler))
    assert analytics.query("SELECT 1", client) == [{"a": 1, "b": 2}]
    assert seen["auth"] == "Bearer phx_test"
    assert seen["url"].endswith("/api/projects/123/query/")


def test_query_errors_without_a_key_or_on_a_bad_status(monkeypatch: Any) -> None:
    monkeypatch.delenv("POSTHOG_PERSONAL_API_KEY", raising=False)
    with pytest.raises(analytics.AnalyticsError):
        analytics.query("SELECT 1")
    monkeypatch.setenv("POSTHOG_PERSONAL_API_KEY", "phx_test")
    monkeypatch.setenv("POSTHOG_PROJECT_ID", "123")
    client = httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(403, text="no")))
    with pytest.raises(analytics.AnalyticsError, match="403"):
        analytics.query("SELECT 1", client)
