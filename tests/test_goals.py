from __future__ import annotations

from datetime import date

from daylog.goals import (
    apply_confirmed_slip,
    apply_progress,
    apply_slips,
    find_goal,
)

ON = date(2026, 8, 21)


def _goals() -> list[dict[str, object]]:
    return [
        {
            "id": "jobs",
            "title": "Job applications",
            "type": "hard",
            "metric": "applications_sent",
            "progress": 3,
            "status": "active",
        },
        {
            "id": "surf",
            "title": "Get cracked at surfing",
            "type": "soft",
            "metric": "hours",
            "status": "active",
            "slip_history": [],
        },
        {
            "id": "paragliding",
            "title": "Paragliding license",
            "type": "soft",
            "status": "active",
            "target_window": ["2026-09-01", "2026-10-01"],
            "slip_history": [],
        },
    ]


def test_find_goal() -> None:
    goals = _goals()
    goal = find_goal(goals, "jobs")
    assert goal is not None
    assert goal["title"] == "Job applications"
    assert find_goal(goals, "nonexistent") is None


def test_apply_progress_accumulates_on_existing_value() -> None:
    goals = _goals()
    applied = apply_progress(goals, [{"goal_id": "jobs", "delta": 2}])

    assert len(applied) == 1
    assert applied[0].new_progress == 5
    goal = find_goal(goals, "jobs")
    assert goal is not None
    assert goal["progress"] == 5


def test_apply_progress_initializes_missing_progress_field() -> None:
    goals = _goals()
    applied = apply_progress(goals, [{"goal_id": "surf", "delta": 2.5}])

    assert applied[0].new_progress == 2.5
    goal = find_goal(goals, "surf")
    assert goal is not None
    assert goal["progress"] == 2.5


def test_apply_progress_ignores_unknown_goal_id() -> None:
    goals = _goals()
    applied = apply_progress(goals, [{"goal_id": "nonexistent", "delta": 5}])
    assert applied == []


def test_apply_progress_ignores_missing_delta() -> None:
    goals = _goals()
    applied = apply_progress(goals, [{"goal_id": "jobs"}])
    assert applied == []


def test_apply_slips_soft_goal_applies_immediately() -> None:
    goals = _goals()
    applied, pending = apply_slips(
        goals, [{"goal_id": "paragliding", "new_date": "2026-11-01", "reason": "weather"}], on=ON
    )

    assert pending == []
    assert len(applied) == 1
    goal = find_goal(goals, "paragliding")
    assert goal is not None
    # start date preserved as-is (not touched); only the end date moves, and
    # moved/appended dates are stored as real `date` objects (see goals.py's
    # _write_slip) so the YAML dumps unquoted like a hand-typed date.
    assert goal["target_window"] == ["2026-09-01", date(2026, 11, 1)]
    assert goal["slip_history"] == [
        {"from": date(2026, 10, 1), "to": date(2026, 11, 1), "on": ON, "reason": "weather"}
    ]


def test_apply_slips_soft_goal_with_no_existing_window() -> None:
    goals = _goals()
    applied, pending = apply_slips(goals, [{"goal_id": "surf", "new_date": "2026-12-01"}], on=ON)

    assert len(applied) == 1
    goal = find_goal(goals, "surf")
    assert goal is not None
    assert goal["target_window"] == [date(2026, 12, 1), date(2026, 12, 1)]
    assert goal["slip_history"][0]["from"] is None
    assert "reason" not in goal["slip_history"][0]


def test_apply_slips_hard_goal_is_held_pending_not_applied() -> None:
    goals = _goals()
    applied, pending = apply_slips(
        goals, [{"goal_id": "jobs", "new_date": "2026-12-31", "reason": "busy"}], on=ON
    )

    assert applied == []
    assert len(pending) == 1
    assert pending[0].goal_id == "jobs"
    assert pending[0].new_date == "2026-12-31"
    assert pending[0].old_date is None  # deadline was never set
    # crucially: the goal itself is untouched until confirmed
    goal = find_goal(goals, "jobs")
    assert goal is not None
    assert "deadline" not in goal
    assert "slip_history" not in goal


def test_apply_slips_ignores_unknown_goal_id() -> None:
    goals = _goals()
    applied, pending = apply_slips(goals, [{"goal_id": "nope", "new_date": "2026-01-01"}], on=ON)
    assert applied == [] and pending == []


def test_apply_confirmed_slip_writes_hard_deadline() -> None:
    goals = _goals()
    _, pending = apply_slips(
        goals, [{"goal_id": "jobs", "new_date": "2026-12-31", "reason": "busy"}], on=ON
    )
    slip = pending[0]

    apply_confirmed_slip(goals, slip, on=ON)

    goal = find_goal(goals, "jobs")
    assert goal is not None
    assert goal["deadline"] == date(2026, 12, 31)
    assert goal["slip_history"] == [
        {"from": None, "to": date(2026, 12, 31), "on": ON, "reason": "busy"}
    ]
