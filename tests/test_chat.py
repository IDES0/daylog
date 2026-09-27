from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime
from types import SimpleNamespace
from typing import Any

from daylog import chat
from daylog.chat_flow import apply_proposal
from daylog.vault import Vault

from .place_fixtures import tree

NOW = datetime(2026, 9, 27, 18, 0)


def _seed(vault: Vault) -> None:
    nodes = tree()
    vault.write_place_files({"all": nodes}, {"all"}, "places: seed")
    vault.write_journal_entry(
        datetime(2026, 9, 26, 20, 0),
        {"activities": [{"type": "surf", "place": "ekas-bay"}], "meals": [{"items": ["ramen"]}]},
        "t",
        "Surfed Ekas.",
    )
    vault.write_yaml(
        "location.yaml",
        [{"place": "Kuta, Lombok, ID", "from": date(2026, 9, 1), "to": None}],
        "loc",
    )
    vault.write_yaml("rankings.yaml", {"surf": {"liked": ["ekas-bay"]}}, "rank")


def _block(**kw: Any) -> Any:
    ns = SimpleNamespace(**kw)
    ns.model_dump = lambda exclude_none=True: dict(kw)
    return ns


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


def _client(fake: FakeMessages) -> Any:
    return SimpleNamespace(messages=fake)


def test_tools_read_the_vault(vault: Vault) -> None:
    _seed(vault)
    tools = chat.Tools(vault, NOW.date())
    assert "Surfed Ekas." in tools.run("get_journal", {"start": "2026-09-20", "end": "2026-09-27"})
    assert "Sep 26: Ekas Bay (surf_spot)" in tools.run(
        "get_trail", {"start": "2026-09-25", "end": "2026-09-27"}
    )
    assert "kuta-bali (town): Kuta, Bali, Indonesia" in tools.run("find_places", {"query": "kuta"})
    place = tools.run("get_place", {"place_id": "ekas-bay"})
    assert '"visited": ["2026-09-26"]' in place and "#1 of 1 surf" in place
    assert "1. Ekas Bay, Lombok, Indonesia" in tools.run("get_rankings", {"category": "surf"})
    assert tools.run("nope", {}) == "unknown tool nope"
    assert tools.run("get_place", {"wrong": 1}).startswith("bad arguments")


def test_add_place_note_writes_immediately(vault: Vault) -> None:
    _seed(vault)
    chat.Tools(vault, NOW.date()).run(
        "add_place_note", {"place_id": "ekas-bay", "text": "best on a mid tide"}
    )
    node = next(n for n in vault.read_places() if n["id"] == "ekas-bay")
    assert node["my_notes"][0]["text"] == "best on a mid tide"


def test_respond_runs_tools_and_keeps_only_words_in_history(vault: Vault) -> None:
    _seed(vault)
    fake = FakeMessages(
        [
            SimpleNamespace(
                content=[
                    _block(
                        type="tool_use", id="t1", name="get_rankings", input={"category": "surf"}
                    ),
                    _block(
                        type="tool_use",
                        id="t2",
                        name="propose_itinerary_change",
                        input={
                            "place": "Mentawai",
                            "status": "candidate",
                            "summary": "Add Mentawai",
                        },
                    ),
                ],
                stop_reason="tool_use",
                usage=_usage(),
            ),
            SimpleNamespace(
                content=[_block(type="text", text="Ekas is your top wave. Proposed Mentawai.")],
                stop_reason="end_turn",
                usage=_usage(),
            ),
        ]
    )
    result = chat.respond(vault, [], "best wave?", NOW, allow_web=False, client=_client(fake))
    assert result.text == "Ekas is your top wave. Proposed Mentawai."
    assert result.proposals == [
        {"place": "Mentawai", "status": "candidate", "summary": "Add Mentawai"}
    ]
    second_call = fake.calls[1]["messages"]
    tool_results = second_call[-1]["content"]
    assert tool_results[0]["tool_use_id"] == "t1" and "Ekas Bay" in tool_results[0]["content"]
    assert "<context>" in second_call[0]["content"]
    assert result.new_history == [
        {"role": "user", "content": "best wave?"},
        {"role": "assistant", "content": "Ekas is your top wave. Proposed Mentawai."},
    ]
    assert all(t.get("name") != "web_search" for t in fake.calls[0]["tools"])


def test_trim_history_starts_on_a_user_text_turn() -> None:
    history: list[Any] = [
        {"role": "user", "content": "a"},
        {"role": "assistant", "content": "b"},
        {"role": "user", "content": "c"},
        {"role": "assistant", "content": "d"},
    ]
    assert chat.trim_history(history, limit=3) == history[2:]


def test_apply_proposal_applies_hard_dates_without_a_second_prompt() -> None:
    data: list[Any] = [{"id": "visa", "place": "Visa exit", "type": "hard", "status": "planned"}]
    outcome = apply_proposal(
        data, {"id": "visa", "new_date": "2026-10-20", "summary": "x"}, date(2026, 9, 27)
    )
    assert data[0]["deadline"] == date(2026, 10, 20)
    assert "2026-10-20" in outcome
