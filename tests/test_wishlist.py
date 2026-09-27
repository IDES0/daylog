from __future__ import annotations

from datetime import date
from typing import Any

from daylog import itinerary, wishlist_flow
from daylog.places import PlaceIndex
from daylog.vault import Vault

from .place_fixtures import tree

TODAY = date(2026, 9, 27)

DOSSIER = """# Mentawai
_Researched 2026-09-27_

## Why go
Rights.

## When
Peak season Apr-Oct; October still pumping.

## Events
None found.

## Costs
Boat trips $$$.
"""


def test_dossier_digest_keeps_only_timing_sections() -> None:
    digest = wishlist_flow.dossier_digest(DOSSIER)
    assert digest == "When: Peak season Apr-Oct; October still pumping.\nEvents: None found."


def test_new_itinerary_entry_links_place_and_queues_research() -> None:
    data: list[Any] = []
    itinerary.apply_itinerary_changes(
        data,
        [{"place": "Mentawai", "place_id": "mentawai", "why": ["surf"], "status": "candidate"}],
        on=TODAY,
    )
    assert data[0]["place_id"] == "mentawai"
    assert data[0]["why"] == ["surf"]
    assert itinerary.wants_research(data[0])


def test_add_wish_creates_then_reactivates(vault: Vault) -> None:
    nodes = tree()
    vault.write_place_files({"all": nodes}, {"all"}, "places: seed")
    entry, created = wishlist_flow.add_wish(vault, "labuan bajo", TODAY)
    assert created and entry["place_id"] == "labuan-bajo" and entry["research"] == "queued"

    data = vault.read_itinerary()
    data[0]["status"] = "dropped"
    vault.write_itinerary(data, "drop")
    entry, created = wishlist_flow.add_wish(vault, "LBJ", TODAY)
    assert not created and entry["status"] == "candidate"
    assert len(vault.read_itinerary()) == 1


def test_format_wishlist_shows_path_why_and_research() -> None:
    data = [
        {
            "id": "a",
            "place": "Labuan Bajo",
            "place_id": "labuan-bajo",
            "status": "candidate",
            "why": ["dive"],
            "research": "done",
            "dossier": "research/labuan-bajo.md",
        },
        {"id": "b", "place": "Old", "status": "done"},
    ]
    text = wishlist_flow.format_wishlist(data, PlaceIndex(tree()))
    assert "Labuan Bajo [candidate] · for dive" in text
    assert "Labuan Bajo, Flores, Indonesia · research: done (research/labuan-bajo.md)" in text
    assert "Old" not in text


def test_dossiers_for_reads_linked_research_files(vault: Vault) -> None:
    vault.write_text("research/mentawai.md", DOSSIER, "research: mentawai")
    data = [{"id": "m", "place": "Mentawai", "place_id": "mentawai", "status": "planned"}]
    assert wishlist_flow.dossiers_for(vault, data) == {"Mentawai": DOSSIER}
