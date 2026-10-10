from __future__ import annotations

from typing import Any

import httpx
import pytest

from daylog import analytics


@pytest.fixture(autouse=True)
def _configured(monkeypatch: Any) -> None:
    monkeypatch.setenv("POSTHOG_PERSONAL_API_KEY", "phx_test")
    monkeypatch.setenv("POSTHOG_PROJECT_ID", "123")


def _row(ref: str = "acme") -> dict:
    return {
        "person_id": "p1",
        "ref": ref,
        "first_seen": "2026-10-10T11:40:00.150000Z",
        "pages": ["/resume", "/"],
        "resume": 1,
        "chess": 0,
        "spacetime": 3,
        "city": "Austin",
        "country": "US",
        "session": "s-123",
    }


def _summary() -> dict:
    return analytics.summarize(
        [{"people": 5, "views": 12, "resume": 1, "chess": 2, "spacetime": 0}],
        [
            {"key": "", "people": 3, "n": 6},
            {"key": "linkedin", "people": 1, "n": 2},
            {"key": "acme", "people": 1, "n": 4},
        ],
    )


def test_visit_line_has_pages_actions_place_and_recording() -> None:
    text = analytics.format_visit(analytics.visits([_row()])[0])
    assert text.startswith("• acme: 2 pages (/, /resume)")
    assert "tried resume, spacetime" in text and "Austin, US" in text
    assert "https://us.posthog.com/project/123/replay/s-123" in text


def test_brief_block_lists_company_refs_and_points_to_site() -> None:
    summary = _summary()
    assert summary["company_refs"] == ["acme"]
    text = analytics.format_brief(summary, analytics.visits([_row()]))
    assert "Visitors: 5 (12 page views)" in text
    assert "By ref: (none) 3 · linkedin 1 · acme 1" in text
    assert "Company refs:\n• acme:" in text
    assert text.endswith("/site for more")


def test_quiet_day_is_one_line() -> None:
    assert analytics.format_brief(analytics.summarize([], []), []) == (
        "Site (last 24 h): no visitors."
    )


def test_vault_record_keeps_summaries_not_identities() -> None:
    state = analytics.record_day({}, "2026-10-10", _summary(), analytics.visits([_row()]))
    text = repr(state)
    assert "p1" not in text and "Austin" not in text and "s-123" not in text
    assert state["days"]["2026-10-10"]["company_visits"] == [
        {
            "ref": "acme",
            "pages": 2,
            "resume": True,
            "chess": False,
            "spacetime": True,
            "country": "US",
        }
    ]


def test_record_day_keeps_a_bounded_history() -> None:
    state: dict = {}
    for month in range(1, 7):
        for d in range(1, 29):
            state = analytics.record_day(state, f"2026-{month:02d}-{d:02d}", {"people": 1}, [])
    assert len(state["days"]) == analytics.KEEP_DAYS
    assert max(state["days"]) == "2026-06-28"


def test_queries_exclude_tests_and_channels() -> None:
    q = analytics.visits_hogql(7)
    assert "INTERVAL 7 DAY" in q
    assert "'linkedin', 'github', 'test', 'claude-test'" in q
    assert "distinct_id != 'claude-test'" in analytics.totals_hogql(1)


def _fake(responses: dict[str, dict]) -> httpx.Client:
    """Answers each query by the first marker found in its HogQL."""

    def handler(request: httpx.Request) -> httpx.Response:
        hogql = request.read().decode()
        for marker, body in responses.items():
            if marker in hogql:
                return httpx.Response(200, json=body)
        return httpx.Response(200, json={"columns": [], "results": []})

    return httpx.Client(transport=httpx.MockTransport(handler))


def test_report_reads_every_section() -> None:
    client = _fake(
        {
            "uniqIf(person_id, event = '$pageview') AS people": {
                "columns": ["people", "views", "resume", "chess", "spacetime"],
                "results": [[2, 5, 1, 1, 0]],
            },
            "toDate(timestamp)": {
                "columns": ["key", "people", "n"],
                "results": [["2026-10-09", 1, 2], ["2026-10-08", 1, 3]],
            },
            "$pathname AS key": {"columns": ["key", "people", "n"], "results": [["/", 2, 3]]},
            "AS games": {
                "columns": ["games", "moves", "engine_s", "sources", "exports"],
                "results": [[1, 14, 2.34, 0, 0]],
            },
            "argMin": {
                "columns": list(_row()),
                "results": [list(_row().values())],
            },
        }
    )
    report = analytics.fetch_report(7, client)
    assert report.startswith("Site, last 7 days\nVisitors: 2 (5 page views)")
    assert report.index("2026-10-08") < report.index("2026-10-09")
    assert "Top pages\n  /: 2 (3)" in report
    assert "chess: 1 games, 14 moves, engine loads in 2.3 s" in report
    assert "Company refs\n2026-10-10 acme: 2 pages" in report


def test_report_on_an_empty_site() -> None:
    assert analytics.fetch_report(500, _fake({})) == "Site, last 90 days: no visitors."


def test_query_sends_bearer_key_and_maps_columns() -> None:
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
    client = httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(403, text="no")))
    with pytest.raises(analytics.AnalyticsError, match="403"):
        analytics.query("SELECT 1", client)
    monkeypatch.delenv("POSTHOG_PERSONAL_API_KEY")
    with pytest.raises(analytics.AnalyticsError):
        analytics.query("SELECT 1")
