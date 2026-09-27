from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime
from types import SimpleNamespace
from typing import Any

from daylog import llm, research, research_flow
from daylog.places import PlaceIndex
from daylog.vault import Vault

from .place_fixtures import tree

TODAY = date(2026, 9, 27)


def _usage(inp: int = 1000, out: int = 100, searches: int = 0) -> Any:
    return SimpleNamespace(
        input_tokens=inp,
        output_tokens=out,
        cache_creation_input_tokens=0,
        cache_read_input_tokens=0,
        server_tool_use=SimpleNamespace(web_search_requests=searches),
    )


def _tool_use(payload: dict[str, Any]) -> Any:
    return SimpleNamespace(type="tool_use", name="record_places", input=payload)


@dataclass
class FakeMessages:
    responses: list[Any]
    calls: list[dict[str, Any]] = field(default_factory=list)

    def create(self, **kwargs: Any) -> Any:
        self.calls.append(kwargs)
        return self.responses.pop(0)


def _client(*responses: Any) -> Any:
    return SimpleNamespace(messages=FakeMessages(list(responses)))


def test_cost_of_counts_tokens_cache_and_searches() -> None:
    usage = SimpleNamespace(
        input_tokens=1_000_000,
        output_tokens=100_000,
        cache_creation_input_tokens=0,
        cache_read_input_tokens=1_000_000,
        server_tool_use=SimpleNamespace(web_search_requests=5),
    )
    # 1M in @ $2 + 1M cache read @ $0.2 + 100k out @ $10/M + 5 searches
    assert abs(llm.cost_of("claude-sonnet-5", usage) - (2.0 + 0.2 + 1.0 + 0.05)) < 1e-9


def test_flush_accumulates_into_usage_yaml(vault: Vault) -> None:
    llm._pending.clear()
    llm.record("extract", "claude-sonnet-5", _usage(1_000_000, 0))
    llm.record("research", "claude-sonnet-5", _usage(0, 0, searches=10))
    assert abs(llm.month_spend(vault) - 2.1) < 1e-9
    llm.flush(vault)
    assert llm._pending == []
    month = vault.read_yaml(llm.USAGE_FILE, {})[date.today().strftime("%Y-%m")]
    assert month["extract"] == 2.0 and month["research"] == 0.1 and month["total"] == 2.1
    assert "$2.10" in llm.format_usage(vault)


def test_parse_drops_invented_ids_and_parentless_places() -> None:
    index = PlaceIndex(tree())
    raw = {
        "matches": [
            {"mention": "the peak", "place_id": "ekas-bay"},
            {"mention": "x", "place_id": "made-up"},
        ],
        "new_places": [
            {"ref": "a", "name": "Warung A", "kind": "food", "parent_id": "kuta"},
            {"ref": "b", "name": "Orphan", "kind": "food"},
            {"ref": "c", "name": "Bad kind", "kind": "castle", "parent_id": "kuta"},
            {"ref": "d", "name": "Cafe", "kind": "food", "parent_ref": "a"},
        ],
        "summary": "ok",
    }
    findings = research._parse(raw, index)
    assert findings.matches == [{"mention": "the peak", "place_id": "ekas-bay"}]
    assert [p["name"] for p in findings.new_places] == ["Warung A", "Cafe"]


def test_run_continues_through_pause_turn_then_returns_findings() -> None:
    client = _client(
        SimpleNamespace(content=[], stop_reason="pause_turn", usage=_usage(searches=3)),
        SimpleNamespace(
            content=[_tool_use({"new_places": [], "summary": "nothing new"})],
            stop_reason="tool_use",
            usage=_usage(),
        ),
    )
    findings = research.run("JOB: resolve", PlaceIndex(tree()), None, client=client)
    assert findings.summary == "nothing new"
    assert len(client.messages.calls) == 2
    assert "tool_choice" not in client.messages.calls[1]


def test_run_forces_the_record_call_when_the_model_just_talks() -> None:
    client = _client(
        SimpleNamespace(content=[], stop_reason="end_turn", usage=_usage()),
        SimpleNamespace(
            content=[_tool_use({"new_places": [], "summary": "forced"})],
            stop_reason="tool_use",
            usage=_usage(),
        ),
    )
    findings = research.run("JOB", PlaceIndex(tree()), None, client=client)
    assert findings.summary == "forced"
    assert client.messages.calls[1]["tool_choice"] == {"type": "tool", "name": "record_places"}


def test_run_stops_searching_once_over_budget() -> None:
    client = _client(
        SimpleNamespace(content=[], stop_reason="pause_turn", usage=_usage(searches=50)),
        SimpleNamespace(
            content=[_tool_use({"new_places": [], "summary": "capped"})],
            stop_reason="tool_use",
            usage=_usage(),
        ),
    )
    findings = research.run("JOB", PlaceIndex(tree()), None, budget=0.25, client=client)
    assert findings.summary == "capped"
    assert "tool_choice" in client.messages.calls[1]


def test_apply_new_places_creates_parents_first_and_avoids_id_collisions() -> None:
    files: dict[str, Any] = {"lombok": [n for n in tree() if n["id"] != "indonesia"]}
    files["_world"] = [tree()[0]]
    created = research.apply_new_places(
        files,
        [
            {"ref": "cafe", "name": "Cafe Kuta", "kind": "food", "parent_ref": "town"},
            {"ref": "town", "name": "Ekas Bay", "kind": "town", "parent_id": "lombok"},
        ],
        TODAY,
    )
    assert created == {"town": "ekas-bay-lombok", "cafe": "cafe-kuta"}
    index = PlaceIndex([n for nodes in files.values() for n in nodes])
    assert index.by_id["cafe-kuta"]["parent"] == "ekas-bay-lombok"


def test_link_mentions_and_unresolved_mentions() -> None:
    fm: dict[str, Any] = {
        "activities": [
            {"type": "surf", "place_mention": "Cobble Stones", "place_kind": "surf_spot"}
        ],
        "meals": [{"items": ["pizza"], "place_mention": "Pizza Place"}],
    }
    mentions = research.unresolved_mentions(TODAY, fm)
    assert [m.text for m in mentions] == ["Cobble Stones", "Pizza Place"]
    assert research.link_mentions(fm, {"cobble stones": "cobblestone"}) == 1
    assert fm["activities"][0] == {"type": "surf", "place": "cobblestone"}
    assert [m.text for m in research.unresolved_mentions(TODAY, fm)] == ["Pizza Place"]


def _seed(vault: Vault) -> None:
    nodes = tree()
    files = {"_world": [nodes[0]], "lombok": nodes[1:6], "bali": nodes[6:9], "flores": nodes[9:]}
    vault.write_place_files(files, set(files), "places: seed")
    vault.write_journal_entry(
        datetime(2026, 9, 27, 20, 0),
        {
            "activities": [{"type": "surf", "place_mention": "the inside"}],
            "meals": [{"items": ["nasi"], "place_mention": "Warung Sunset"}],
        },
        "t",
        "s",
    )


def test_apply_matches_links_journal_and_learns_alias(vault: Vault) -> None:
    _seed(vault)
    linked = research_flow.apply_matches(
        vault, [{"mention": "the inside", "place_id": "ekas-bay"}], [TODAY]
    )
    assert linked == ["the inside → Ekas Bay"]
    entry = vault.read_journal_entry(TODAY)
    assert entry is not None and entry.frontmatter["activities"][0]["place"] == "ekas-bay"
    assert "the inside" in PlaceIndex(vault.read_places()).by_id["ekas-bay"]["aliases"]


def test_add_proposals_writes_place_and_links_mention(vault: Vault) -> None:
    _seed(vault)
    batch: dict[str, Any] = {
        "proposals": [
            {
                "ref": "w",
                "mention": "Warung Sunset",
                "name": "Warung Sunset",
                "kind": "food",
                "parent_id": "kuta",
                "confidence": "inferred",
                "description": "Local warung.",
            }
        ],
        "days": [TODAY.isoformat()],
    }
    assert research_flow.add_proposals(vault, batch, [0]) == ["warung-sunset"]
    assert research_flow.add_proposals(vault, batch, [0]) == []  # second tap is a no-op
    index = PlaceIndex(vault.read_places())
    assert index.path_name("warung-sunset") == "Warung Sunset, Kuta, Lombok, Indonesia"
    entry = vault.read_journal_entry(TODAY)
    assert entry is not None and entry.frontmatter["meals"][0]["place"] == "warung-sunset"


def test_add_trip_stops_appends_visits_once(vault: Vault) -> None:
    _seed(vault)
    research_flow._add_trip_stops(vault, TODAY, ["labuan-bajo", "labuan-bajo"])
    research_flow._add_trip_stops(vault, TODAY, ["labuan-bajo"])
    entry = vault.read_journal_entry(TODAY)
    assert entry is not None
    visits = [a for a in entry.frontmatter["activities"] if a.get("type") == "visit"]
    assert visits == [{"type": "visit", "place": "labuan-bajo"}]
