from __future__ import annotations

from datetime import date, datetime
from typing import Any
from zoneinfo import ZoneInfo

import pytest

from daylog import (
    brief,
    daily,
    llm,
    plan_flow,
    planner,
    push,
    research,
    research_flow,
    scheduler,
    services,
)
from daylog.vault import Vault
from daylog.webapp import api as webapi

TZ = ZoneInfo("Asia/Makassar")
# A Saturday.
NOON = datetime(2026, 10, 24, 12, 0, tzinfo=TZ)


@pytest.fixture
def sent(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, str, str]]:
    out: list[tuple[str, str, str]] = []

    def fake_send(v: Vault, title: str, body: str, url: str = "/#/home") -> int:
        out.append((title, body, url))
        return 1

    monkeypatch.setattr(push, "send", fake_send)
    return out


@pytest.fixture
def api(vault: Vault) -> webapi.Api:
    return webapi.Api(vault=lambda: vault, spawn=lambda fn: fn(), run_notes=lambda fn: fn())


# -- the clock ------------------------------------------------------------------------


def test_jobs_fire_at_the_configured_local_hours(
    monkeypatch: pytest.MonkeyPatch, api: webapi.Api
) -> None:
    monkeypatch.setenv("TZ", "Asia/Makassar")
    monkeypatch.setenv("BRIEF_HOUR", "6")
    monkeypatch.setenv("DAY_CUTOFF_HOUR", "3")
    monkeypatch.setenv("REVIEW_HOUR", "19")
    clock = scheduler.build(api)
    fires = {
        job.id: job.trigger.get_next_fire_time(None, NOON).replace(tzinfo=None)
        for job in clock.get_jobs()
    }
    assert fires == {
        "brief": datetime(2026, 10, 25, 6, 0),
        "reconcile": datetime(2026, 10, 25, 3, 5),
        "weekly": datetime(2026, 10, 25, 19, 0),  # Sunday
    }
    weekly = next(j for j in clock.get_jobs() if j.id == "weekly")
    after = weekly.trigger.get_next_fire_time(None, datetime(2026, 10, 25, 20, 0, tzinfo=TZ))
    assert after.replace(tzinfo=None) == datetime(2026, 11, 1, 19, 0)
    assert str(weekly.trigger.timezone) == "Asia/Makassar"


def test_each_job_calls_its_function_with_the_shared_api(
    monkeypatch: pytest.MonkeyPatch, api: webapi.Api
) -> None:
    called: list[tuple[str, Any]] = []
    for name in ("morning_brief", "end_of_day", "weekly"):
        monkeypatch.setattr(scheduler, name, lambda a, n=name: called.append((n, a)))
    for _, func, _ in scheduler.jobs(api):
        func()
    assert called == [("morning_brief", api), ("end_of_day", api), ("weekly", api)]


# -- the brief ------------------------------------------------------------------------


def _fake_brief_inputs(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(services, "_last_brief", None)
    monkeypatch.setattr(services, "conditions_reports", lambda *a: (None, None))
    monkeypatch.setattr(services, "fetch_marine_forecast", lambda *a: None)
    monkeypatch.setattr(services, "fetch_wind_forecast", lambda *a: None)
    monkeypatch.setattr(brief, "generate_brief", lambda **kw: "## Today\nSurf at 7.")


def test_morning_brief_pushes_and_is_saved_for_after_a_restart(
    monkeypatch: pytest.MonkeyPatch,
    vault: Vault,
    api: webapi.Api,
    sent: list[tuple[str, str, str]],
) -> None:
    _fake_brief_inputs(monkeypatch)
    scheduler.morning_brief(api, vault, NOON)
    assert sent == [("☀️ Brief · Sat Oct 24", "Today · Surf at 7.", "/#/home")]
    assert vault.read_text("briefs/2026-10-24.md") == "## Today\nSurf at 7.\n"
    assert "Surf at 7." in str(api.chat.history())

    monkeypatch.setattr(services, "_last_brief", None)  # a restart
    last = services.last_brief(vault)
    assert last is not None and last.date == date(2026, 10, 24)
    assert last.text == "## Today\nSurf at 7."


def test_morning_brief_says_how_many_decisions_wait(
    monkeypatch: pytest.MonkeyPatch,
    vault: Vault,
    api: webapi.Api,
    sent: list[tuple[str, str, str]],
) -> None:
    _fake_brief_inputs(monkeypatch)
    monkeypatch.setattr(api, "waiting_decisions", lambda v: 2)
    scheduler.morning_brief(api, vault, NOON)
    assert [s[0] for s in sent][1] == "2 decisions waiting"
    assert sent[1][2] == "/#/home"


def test_a_failed_brief_is_one_notification(
    monkeypatch: pytest.MonkeyPatch,
    vault: Vault,
    api: webapi.Api,
    sent: list[tuple[str, str, str]],
) -> None:
    def boom(v: Vault, today: date) -> str:
        raise RuntimeError("down")

    monkeypatch.setattr(services, "build_brief", boom)
    scheduler.morning_brief(api, vault, NOON)
    assert [s[0] for s in sent] == ["Brief failed"]


# -- reconcile ------------------------------------------------------------------------


def test_reconcile_is_quiet_unless_a_goal_total_was_corrected(
    monkeypatch: pytest.MonkeyPatch,
    vault: Vault,
    api: webapi.Api,
    sent: list[tuple[str, str, str]],
) -> None:
    seen: list[date] = []

    def recent(v: Vault, today: date) -> list[str]:
        seen.append(today)
        return ["2026-10-23 reconciled (2 notes)."]

    monkeypatch.setattr(daily, "reconcile_recent", recent)
    scheduler.end_of_day(api, vault, datetime(2026, 10, 24, 4, 5, tzinfo=TZ))
    assert seen == [date(2026, 10, 24)] and sent == []

    monkeypatch.setattr(
        daily,
        "reconcile_recent",
        lambda v, t: ["2026-10-23 reconciled (2 notes).\nGoal totals corrected: surf 3h"],
    )
    scheduler.end_of_day(api, vault, NOON)
    assert len(sent) == 1 and sent[0][0] == "Goal totals corrected"
    assert "surf 3h" in sent[0][1] and sent[0][2] == "/#/you"


# -- weekly review + plan ---------------------------------------------------------------


def _fake_review(monkeypatch: pytest.MonkeyPatch) -> None:
    week = (date(2026, 10, 19), date(2026, 10, 25))
    monkeypatch.setattr(
        daily, "write_review", lambda v, d: ("2026-W43", *week, "A good week of surf.")
    )


def test_weekly_review_then_plan_card(
    monkeypatch: pytest.MonkeyPatch,
    vault: Vault,
    api: webapi.Api,
    sent: list[tuple[str, str, str]],
) -> None:
    _fake_review(monkeypatch)
    monkeypatch.setattr(llm, "can_spend", lambda v, estimate=0.0: True)
    plan = planner.Plan("You're in Bali.", [{"label": "Stay"}, {"label": "Go to Lombok"}], 1)
    monkeypatch.setattr(
        plan_flow, "generate", lambda v, now: (plan, "# Plan", "plans/2026-10-25.md")
    )
    scheduler.weekly(api, vault, NOON)
    assert sent == [
        ("Weekly review is ready · 2026-W43", "A good week of surf.", "/#/you"),
        ("A plan for the next weeks", "2 options to pick from.", "/#/trips"),
    ]
    cards = [c for c in api.cards.open() if c.kind == "plan"]
    assert len(cards) == 1
    assert [a["id"] for a in cards[0].actions] == ["0", "1", "hold"]


def test_weekly_plan_skipped_on_budget_says_so_in_the_review_push(
    monkeypatch: pytest.MonkeyPatch,
    vault: Vault,
    api: webapi.Api,
    sent: list[tuple[str, str, str]],
) -> None:
    _fake_review(monkeypatch)
    monkeypatch.setattr(llm, "can_spend", lambda v, estimate=0.0: False)

    def no_plan(v: Vault, now: datetime) -> Any:
        raise AssertionError("no plan over budget")

    monkeypatch.setattr(plan_flow, "generate", no_plan)
    scheduler.weekly(api, vault, NOON)
    assert len(sent) == 1
    assert sent[0][1].startswith("No plan this week: the monthly budget is spent.")


def test_weekly_with_no_entries_sends_nothing(
    monkeypatch: pytest.MonkeyPatch,
    vault: Vault,
    api: webapi.Api,
    sent: list[tuple[str, str, str]],
) -> None:
    monkeypatch.setattr(daily, "write_review", lambda v, d: None)
    scheduler.weekly(api, vault, NOON)
    assert sent == []


# -- research found new places after a note ----------------------------------------------


def test_new_places_from_a_note_are_one_push(
    monkeypatch: pytest.MonkeyPatch,
    vault: Vault,
    api: webapi.Api,
    sent: list[tuple[str, str, str]],
) -> None:
    findings = research.Findings(
        new_places=[
            {"ref": "a", "name": "Warung A", "kind": "food"},
            {"ref": "b", "name": "Warung B", "kind": "food"},
        ]
    )
    monkeypatch.setattr(research_flow, "lookup_mentions", lambda v, days: ("", findings))
    api._mentions_in_background([date(2026, 10, 24)])
    assert sent == [("Places to confirm", "2 new places from your note. Add or skip.", "/#/home")]
    assert len([c for c in api.cards.open() if c.kind == "place"]) == 2

    sent.clear()
    monkeypatch.setattr(research_flow, "lookup_mentions", lambda v, days: (None, None))
    api._mentions_in_background([date(2026, 10, 24)])
    assert sent == []
