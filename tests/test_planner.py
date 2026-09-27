from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime
from types import SimpleNamespace
from typing import Any

from daylog import plan_flow, planner
from daylog.vault import Vault

from .place_fixtures import tree

TODAY = date(2026, 9, 27)

OPTION = {
    "label": "Stay and progress",
    "summary": "Two more weeks at Lakey.",
    "legs": [
        {
            "place": "Lakey",
            "place_id": "lakey",
            "start": "2026-09-27",
            "end": "2026-10-08",
            "why": "coaching",
            "goal_ids": ["surf"],
            "cost_usd": 300,
        },
        {
            "place": "Mentawai",
            "itinerary_id": "mentawai",
            "start": "2026-10-10",
            "end": "2026-10-18",
            "why": "rights",
            "travel": "fly Bima→Bali→Padang, fast ferry",
        },
        {"place": "Bad", "start": "not-a-date", "end": "2026-10-20", "why": "x"},
    ],
    "tradeoffs": "Less variety.",
}


def _usage() -> Any:
    return SimpleNamespace(
        input_tokens=100,
        output_tokens=10,
        cache_creation_input_tokens=0,
        cache_read_input_tokens=0,
        server_tool_use=None,
    )


@dataclass
class FakeMessages:
    responses: list[Any]
    calls: list[dict[str, Any]] = field(default_factory=list)

    def create(self, **kwargs: Any) -> Any:
        self.calls.append(kwargs)
        return self.responses.pop(0)


def test_run_parses_options_and_clamps_recommended() -> None:
    raw = {"situation": "s", "options": [OPTION, {"label": "empty", "legs": []}], "recommended": 5}
    fake = FakeMessages(
        [
            SimpleNamespace(content=[], stop_reason="end_turn", usage=_usage()),
            SimpleNamespace(
                content=[SimpleNamespace(type="tool_use", name="record_plan", input=raw)],
                stop_reason="tool_use",
                usage=_usage(),
            ),
        ]
    )
    plan = planner.run("ctx", client=SimpleNamespace(messages=fake))  # type: ignore[arg-type]
    assert [o["label"] for o in plan.options] == ["Stay and progress"]
    assert plan.recommended == 0
    assert fake.calls[1]["tool_choice"] == {"type": "tool", "name": "record_plan"}


def test_render_marks_recommended_and_lists_legs() -> None:
    text = planner.render(
        planner.Plan("Visa ends Oct 20.", [OPTION], 0, ["Keep the internship?"]), TODAY
    )
    assert "## A. Stay and progress (recommended)" in text
    assert "- 2026-09-27 → 2026-10-08: Lakey · ~$300" in text
    assert "fly Bima→Bali→Padang" in text
    assert "- Keep the internship?" in text


def test_apply_option_sets_planned_windows_and_never_touches_hard_entries() -> None:
    data: list[Any] = [
        {
            "id": "mentawai",
            "place": "Mentawai",
            "place_id": "mentawai",
            "type": "soft",
            "status": "candidate",
        },
        {
            "id": "lakey",
            "place": "Lakey",
            "place_id": "lakey",
            "type": "hard",
            "status": "planned",
            "deadline": date(2026, 10, 20),
        },
    ]
    applied = planner.apply_option(data, OPTION, TODAY)
    assert applied == ["Mentawai: 2026-10-10 → 2026-10-18"]
    assert data[0]["status"] == "planned"
    assert data[0]["target_window"] == [date(2026, 10, 10), date(2026, 10, 18)]
    assert data[1]["deadline"] == date(2026, 10, 20) and "target_window" not in data[1]


def test_apply_option_adds_missing_destinations() -> None:
    data: list[Any] = []
    planner.apply_option(data, {"legs": [OPTION["legs"][1]]}, TODAY)
    assert data[0]["place"] == "Mentawai" and data[0]["status"] == "planned"


def test_build_context_includes_goals_deadlines_and_research(vault: Vault) -> None:
    vault.write_place_files({"all": tree()}, {"all"}, "seed")
    vault.write_goals(
        [{"id": "surf", "title": "Surf", "type": "soft", "metric": "sessions", "progress": 3}], "g"
    )
    vault.write_itinerary(
        [
            {
                "id": "visa",
                "place": "Visa exit",
                "type": "hard",
                "status": "planned",
                "deadline": date(2026, 10, 20),
            },
            {
                "id": "m",
                "place": "Mentawai",
                "place_id": "mentawai",
                "type": "soft",
                "status": "candidate",
            },
        ],
        "i",
    )
    vault.write_text("research/mentawai.md", "# Mentawai\n## When\nApr-Oct\n", "r")
    ctx = plan_flow.build_context(vault, datetime(2026, 9, 27, 9, 0))
    assert "- surf: Surf (soft, progress 3 sessions)" in ctx
    assert "- Visa exit: 2026-10-20" in ctx
    assert "### Mentawai (itinerary_id m, place_id mentawai" in ctx and "Apr-Oct" in ctx
    assert "2026-10-20 (Tuesday)" in ctx
