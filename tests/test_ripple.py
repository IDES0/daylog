from __future__ import annotations

from datetime import date, datetime
from types import SimpleNamespace
from typing import Any

import pytest

from daylog import extract, proposals, reconcile, ripple, ripple_flow
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


def test_analyze_saves_the_analysis_and_a_pending_proposal(
    vault: Vault, monkeypatch: pytest.MonkeyPatch
) -> None:
    _seed(vault)
    seen: dict[str, str] = {}

    def fake_run(context: str, **_: Any) -> ripple.Ripple:
        seen["context"] = context
        return ripple.parse(RAW)

    monkeypatch.setattr(ripple, "run", fake_run)
    path = ripple_flow.analyze(vault, datetime(2026, 10, 15, 9), "took the job")

    assert seen["context"].startswith("The decision, in their words:\ntook the job")
    assert (
        "- nepal-course: Nepal course (soft, planned, 2026-10-24 – 2026-11-07)" in seen["context"]
    )
    assert path == "research/proposals/2026-10-15-accepted-a-six-month-job-in.yaml"
    data = vault.read_yaml(path, {})
    assert data["status"] == "pending" and data["by"] == "bot"
    assert [c["summary"] for c in data["changes"]] == ["Drop Nepal", "Paragliding on hold"]
    assert "# Decision: Accepted" in (vault.read_text(data["analysis"]) or "")
    assert ripple_flow.pending(vault) == [path]


def _proposal(vault: Vault, **extra: Any) -> str:
    path = "research/proposals/2026-10-16-austin.yaml"
    vault.write_yaml(
        path,
        {
            "decision": "Took the Austin job",
            "summary": ["Nepal course: blocked"],
            "changes": RAW["changes"],
            "status": "pending",
            **extra,
        },
        "proposal",
    )
    return path


def test_applying_a_proposal_edits_adopts_the_plan_and_records_it(vault: Vault) -> None:
    _seed(vault)
    vault.write_text("research/proposals/2026-10-16-austin-plan.md", "# New plan\n", "draft")
    path = _proposal(vault, plan_draft="research/proposals/2026-10-16-austin-plan.md")

    outcome = ripple_flow._apply_proposal(vault, path)

    assert outcome.startswith("Applied 3:")
    assert vault.read_itinerary()[0]["status"] == "dropped"
    assert vault.read_goals()[0]["notes"] == "On hold until a US course."
    plans = vault.list_docs("plans")
    assert len(plans) == 1 and vault.read_text(plans[0]) == "# New plan\n"
    assert vault.read_yaml(path, {})["status"] == "applied"
    assert ripple_flow.pending(vault) == []


def test_card_lists_only_changes_to_real_entries(vault: Vault) -> None:
    _seed(vault)
    data = vault.read_yaml(_proposal(vault), {})
    changes = proposals.changes_for(data, {"nepal-course", "japan"}, {"paragliding"})
    text = proposals.card_text(data, changes)
    assert text.startswith("⚡ Took the Austin job\n• Nepal course: blocked")
    assert "1. Drop Nepal\n2. Paragliding on hold" in text
    assert "Drop ghost" not in text and "Ghost goal" not in text


def test_answered_proposals_are_not_pending() -> None:
    assert proposals.is_pending({"status": "pending"})
    assert proposals.is_pending({})
    assert not proposals.is_pending({"status": "skipped"})
    assert not proposals.is_pending(None)


def test_apply_all_reports_what_failed(vault: Vault) -> None:
    _seed(vault)
    done, failed = ripple_flow.apply_all(
        vault, [{"edit": "goal", "summary": "No id, no title"}], TODAY
    )
    assert done == [] and failed == ["No id, no title: a new goal needs a title"]


def test_decisions_are_journaled_and_listed_so_they_trace_once() -> None:
    listing = extract._format_existing_entry({"decisions": ["Took the Austin job"]})
    assert "decisions (already traced, don't repeat):\n  - Took the Austin job" in listing
    assert "decisions" in extract._ARRAY_FIELDS
    assert "decisions" not in reconcile._CAPTURE_ONLY
