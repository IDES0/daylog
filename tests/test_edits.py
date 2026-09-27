from __future__ import annotations

from datetime import date, datetime
from typing import Any

import pytest

from daylog import chat, edits
from daylog.chat_flow import apply_edit
from daylog.places import PlaceIndex
from daylog.vault import Vault, VaultError

from .place_fixtures import tree

TODAY = date(2026, 9, 28)


def _files() -> dict[str, Any]:
    nodes = tree()
    return {"_world": nodes[:1], "lombok": nodes[1:6], "bali": nodes[6:9], "flores": nodes[9:]}


def test_rename_keeps_old_name_as_alias() -> None:
    files = _files()
    changed, summary = edits.edit_place(files, {"place_id": "ramen-otaku", "name": "Otaku Ramen"})
    node = PlaceIndex([n for ns in files.values() for n in ns]).by_id["ramen-otaku"]
    assert node["name"] == "Otaku Ramen" and "Ramen Otaku" in node["aliases"]
    assert changed == {"lombok"} and "renamed Ramen Otaku → Otaku Ramen" in summary


def test_reparent_moves_file_and_refuses_cycles() -> None:
    files = _files()
    changed, _ = edits.edit_place(files, {"place_id": "ramen-otaku", "parent_id": "kuta-bali"})
    assert changed == {"lombok", "bali"}
    assert any(n["id"] == "ramen-otaku" for n in files["bali"])
    with pytest.raises(edits.EditError, match="inside itself"):
        edits.edit_place(files, {"place_id": "lombok", "parent_id": "kuta"})
    with pytest.raises(edits.EditError, match="unknown kind"):
        edits.edit_place(files, {"place_id": "kuta", "kind": "castle"})
    with pytest.raises(edits.EditError, match="nothing to change"):
        edits.edit_place(files, {"place_id": "kuta"})


def test_merge_moves_names_notes_children_and_links() -> None:
    files = _files()
    files["lombok"].append(
        {
            "id": "kuta-dup",
            "name": "Kuta Town",
            "kind": "town",
            "parent": "lombok",
            "my_notes": [{"date": TODAY, "text": "hi"}],
        }
    )
    files["lombok"].append({"id": "warung", "name": "Warung", "kind": "food", "parent": "kuta-dup"})
    _, summary = edits.merge_places(files, "kuta-dup", "kuta")
    index = PlaceIndex([n for ns in files.values() for n in ns])
    assert "kuta-dup" not in index.by_id and "Kuta Town" in index.by_id["kuta"]["aliases"]
    assert index.by_id["warung"]["parent"] == "kuta" and index.by_id["kuta"]["my_notes"]
    fm = {"meals": [{"items": ["x"], "place": "kuta-dup"}]}
    assert edits.relink_journal(fm, "kuta-dup", "kuta") and fm["meals"][0]["place"] == "kuta"
    ranks = {"food": {"liked": ["a", "kuta-dup", "b"]}}
    assert edits.relink_rankings(ranks, "kuta-dup", "kuta") and ranks["food"]["liked"] == [
        "a",
        "kuta",
        "b",
    ]
    ranks = {"food": {"liked": ["kuta", "kuta-dup"]}}
    edits.relink_rankings(ranks, "kuta-dup", "kuta")
    assert ranks["food"]["liked"] == ["kuta"]


def test_delete_refuses_linked_or_parent_places() -> None:
    files = _files()
    with pytest.raises(edits.EditError, match="places inside"):
        edits.delete_place(files, "kuta", [])
    with pytest.raises(edits.EditError, match="merge"):
        edits.delete_place(files, "ramen-otaku", [TODAY])
    changed, _ = edits.delete_place(files, "ramen-otaku", [])
    assert changed == {"lombok"} and all(n["id"] != "ramen-otaku" for n in files["lombok"])


def test_journal_edit_update_remove_add_and_relink() -> None:
    fm: dict[str, Any] = {
        "meals": [{"items": ["bowl"], "place_mention": "Nima", "place_kind": "food"}]
    }
    edits.edit_journal(fm, {"field": "meals", "index": 0, "values": {"place": "kuta"}}, {"kuta"})
    assert fm["meals"][0] == {"items": ["bowl"], "place": "kuta"}
    edits.edit_journal(
        fm, {"field": "activities", "action": "add", "values": {"type": "surf"}}, set()
    )
    edits.edit_journal(fm, {"field": "skipped", "action": "add", "values": {"text": "gym"}}, set())
    assert fm["activities"] == [{"type": "surf"}] and fm["skipped"] == ["gym"]
    edits.edit_journal(fm, {"field": "meals", "action": "remove", "index": 0}, set())
    assert fm["meals"] == []
    with pytest.raises(edits.EditError, match="no place"):
        edits.edit_journal(
            fm, {"field": "activities", "index": 0, "values": {"place": "zzz"}}, set()
        )
    with pytest.raises(edits.EditError, match="no item"):
        edits.edit_journal(fm, {"field": "activities", "index": 5, "values": {}}, set())


def test_location_edit_keeps_order() -> None:
    loc: list[Any] = [
        {"place": "A", "from": date(2026, 9, 1), "to": date(2026, 9, 10)},
        {"place": "C", "from": date(2026, 9, 20), "to": None},
    ]
    edits.edit_location(
        loc, {"action": "add", "place": "B", "from": "2026-09-10", "to": "2026-09-20"}
    )
    assert [e["place"] for e in loc] == ["A", "B", "C"]
    edits.edit_location(loc, {"action": "update", "index": 2, "to": "2026-09-29"})
    assert loc[2]["to"] == date(2026, 9, 29)
    edits.edit_location(loc, {"action": "remove", "index": 1})
    assert [e["place"] for e in loc] == ["A", "C"]


def test_goal_edit_create_and_adjust() -> None:
    data: list[Any] = [{"id": "jobs", "title": "Jobs", "type": "soft", "progress": 40}]
    edits.edit_goal(data, {"goal_id": "jobs", "progress_delta": 10, "date": "2026-10-31"}, TODAY)
    assert data[0]["progress"] == 50 and data[0]["target_window"] == [TODAY, date(2026, 10, 31)]
    edits.edit_goal(data, {"title": "Run 3x a week", "metric": "runs"}, TODAY)
    assert data[1]["id"] == "run-3x-a-week" and data[1]["status"] == "active"
    with pytest.raises(edits.EditError, match="already exists"):
        edits.edit_goal(data, {"title": "Run 3x a week"}, TODAY)


def _seed(vault: Vault) -> None:
    vault.write_place_files(_files(), set(_files()), "places: seed")
    vault.write_journal_entry(
        datetime(2026, 9, 27, 20, 0),
        {"meals": [{"items": ["bowl"], "place": "ramen-otaku"}]},
        "t",
        "s",
    )
    vault.write_yaml("rankings.yaml", {"food": {"liked": ["ramen-otaku"]}}, "rank")


def test_apply_edit_merge_updates_journal_and_rankings(vault: Vault) -> None:
    _seed(vault)
    vault.write_place_files(
        {
            **vault.read_place_files(),
            "lombok": [
                *vault.read_place_files()["lombok"],
                {"id": "nami", "name": "Nami", "kind": "food", "parent": "kuta"},
            ],
        },
        {"lombok"},
        "add nami",
    )
    outcome = apply_edit(
        vault, {"edit": "merge", "source_id": "ramen-otaku", "target_id": "nami"}, TODAY
    )
    assert outcome == "merged Ramen Otaku into Nami"
    entry = vault.read_journal_entry(date(2026, 9, 27))
    assert entry is not None and entry.frontmatter["meals"][0]["place"] == "nami"
    assert vault.read_yaml("rankings.yaml", {})["food"]["liked"] == ["nami"]


def test_preview_rejects_bad_proposals_before_the_card(vault: Vault) -> None:
    _seed(vault)
    tools = chat.Tools(vault, TODAY)
    assert tools.run(
        "propose_delete_place", {"place_id": "ramen-otaku", "summary": "x"}
    ).startswith("Can't propose that")
    assert tools.proposals == []
    ok = tools.run(
        "propose_place_edit", {"place_id": "ramen-otaku", "name": "Nami", "summary": "rename"}
    )
    assert ok.startswith("Proposed") and tools.proposals[0]["edit"] == "place"
    assert vault.read_places()[3]["name"] != "Nami"  # nothing written until confirmed


def test_undo_reverts_a_change_and_refuses_conflicts(vault: Vault) -> None:
    _seed(vault)
    apply_edit(vault, {"edit": "place", "place_id": "ramen-otaku", "name": "Nami"}, TODAY)
    changes = vault.recent_changes()
    assert changes[0][1].startswith("places: Nami: renamed")
    vault.revert(changes[0][0])
    assert any(n["name"] == "Ramen Otaku" for n in vault.read_places())

    apply_edit(vault, {"edit": "place", "place_id": "kuta", "name": "Kuta A"}, TODAY)
    first = vault.recent_changes()[0][0]
    apply_edit(vault, {"edit": "place", "place_id": "kuta", "name": "Kuta B"}, TODAY)
    with pytest.raises(VaultError, match="can't undo"):
        vault.revert(first)
    assert any(n["name"] == "Kuta B" for n in vault.read_places())


def test_place_edit_can_change_the_places_own_kind(vault: Vault) -> None:
    # Regression: the edit type and a place's `kind` once shared a key.
    _seed(vault)
    outcome = apply_edit(
        vault, {"edit": "place", "place_id": "ramen-otaku", "kind": "venue"}, TODAY
    )
    assert "kind food → venue" in outcome
