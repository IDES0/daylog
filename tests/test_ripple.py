from __future__ import annotations

from datetime import date, datetime
from types import SimpleNamespace
from typing import Any

import pytest

from daylog import extract, reconcile, ripple, ripple_flow
from daylog.vault import Vault

from .test_planner import FakeMessages, _usage

TODAY = date(2026, 10, 15)

RAW = {
    "decision": "Accepted a six-month job in Austin from Oct 20",
    "fixes": ["Austin, Mon–Fri 9–5 ET, Oct 20 – Apr 20"],
    "impacts": [
        {
            "target": "nepal-course",
            "label": "Nepal course",
            "effect": "blocked",
            "detail": "Oct 24 – Nov 7 falls in work weeks",
        },
        {"target": "japan", "label": "Japan", "effect": "moved", "detail": "needs time off"},
        {"target": "x", "label": "x", "effect": "nonsense", "detail": "dropped"},
    ],
    "changes": [
        {"edit": "itinerary", "id": "nepal-course", "status": "dropped", "summary": "Drop Nepal"},
        {"edit": "itinerary", "id": "made-up", "status": "dropped", "summary": "Drop ghost"},
        {
            "edit": "goal",
            "goal_id": "paragliding",
            "notes": "On hold until a US course.",
            "summary": "Paragliding on hold",
        },
        {"edit": "goal", "goal_id": "nope", "status": "dropped", "summary": "Ghost goal"},
    ],
    "workarounds": [
        {"goal": "Paragliding", "idea": "P2 course on two weeks off", "when": "spring"},
        {"goal": "", "idea": ""},
    ],
    "opens": ["Skydiving on weekends"],
    "questions": ["Any time off before December?"],
}


def _seed(v: Vault) -> None:
    v.write_itinerary(
        [
            {
                "id": "nepal-course",
                "place": "Nepal course",
                "type": "soft",
                "status": "planned",
                "target_window": [date(2026, 10, 24), date(2026, 11, 7)],
            },
            {
                "id": "japan",
                "place": "Flight to Japan",
                "type": "hard",
                "status": "planned",
                "deadline": date(2026, 12, 27),
            },
        ],
        "itinerary",
    )
    v.write_goals(
        [{"id": "paragliding", "title": "Paragliding", "type": "soft", "status": "active"}],
        "goals",
    )


def test_parse_keeps_known_effects_and_real_workarounds() -> None:
    result = ripple.parse(RAW, 0.4)
    assert [i["effect"] for i in result.impacts] == ["blocked", "moved"]
    assert result.workarounds == [
        {"goal": "Paragliding", "idea": "P2 course on two weeks off", "when": "spring"}
    ]
    assert result.cost == 0.4


def test_valid_changes_only_touch_entries_that_exist() -> None:
    kept = ripple.valid_changes(RAW["changes"], {"nepal-course"}, {"paragliding"})
    assert [c["summary"] for c in kept] == ["Drop Nepal", "Paragliding on hold"]


def test_render_groups_impacts_and_lists_workarounds() -> None:
    text = ripple.render(ripple.parse(RAW), TODAY)
    assert text.startswith("# Decision: Accepted a six-month job in Austin from Oct 20")
    assert "**Blocked**\n- Nepal course: Oct 24 – Nov 7 falls in work weeks" in text
    assert "**Moved**\n- Japan: needs time off" in text
    assert "- Paragliding: P2 course on two weeks off (spring)" in text
    assert "**What it opens up**\n- Skydiving on weekends" in text


def test_run_forces_the_record_tool_when_the_model_stalls() -> None:
    fake = FakeMessages(
        [
            SimpleNamespace(content=[], stop_reason="end_turn", usage=_usage()),
            SimpleNamespace(
                content=[SimpleNamespace(type="tool_use", name="record_ripple", input=RAW)],
                stop_reason="tool_use",
                usage=_usage(),
            ),
        ]
    )
    result = ripple.run("ctx", client=SimpleNamespace(messages=fake))  # type: ignore[arg-type]
    assert result.decision.startswith("Accepted")
    assert fake.calls[1]["tool_choice"] == {"type": "tool", "name": "record_ripple"}


def test_analyze_saves_the_decision_and_filters_changes(
    vault: Vault, monkeypatch: pytest.MonkeyPatch
) -> None:
    _seed(vault)
    seen: dict[str, str] = {}

    def fake_run(context: str, **_: Any) -> ripple.Ripple:
        seen["context"] = context
        return ripple.parse(RAW)

    monkeypatch.setattr(ripple, "run", fake_run)
    result, path = ripple_flow.analyze(vault, datetime(2026, 10, 15, 9), "took the job")

    assert seen["context"].startswith("The decision, in their words:\ntook the job")
    assert (
        "- nepal-course: Nepal course (soft, planned, 2026-10-24 – 2026-11-07)" in seen["context"]
    )
    assert [c["summary"] for c in result.changes] == ["Drop Nepal", "Paragliding on hold"]
    assert path == "research/decisions/2026-10-15-accepted-a-six-month-job-in.md"
    assert "# Decision: Accepted" in (vault.read_text(path) or "")


def test_apply_all_goes_through_the_confirmed_edit_path(vault: Vault) -> None:
    _seed(vault)
    changes = ripple.valid_changes(RAW["changes"], {"nepal-course"}, {"paragliding"})
    changes.append({"edit": "goal", "summary": "No id, no title"})

    done, failed = ripple_flow.apply_all(vault, changes, TODAY)

    assert len(done) == 2 and len(failed) == 1
    assert vault.read_itinerary()[0]["status"] == "dropped"
    assert vault.read_goals()[0]["notes"] == "On hold until a US course."


def test_decisions_are_journaled_and_listed_so_they_trace_once() -> None:
    listing = extract._format_existing_entry({"decisions": ["Took the Austin job"]})
    assert "decisions (already traced, don't repeat):\n  - Took the Austin job" in listing
    assert "decisions" in extract._ARRAY_FIELDS
    assert "decisions" not in reconcile._CAPTURE_ONLY
