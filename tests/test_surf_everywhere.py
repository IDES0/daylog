"""The surf card is never empty where there's ocean; new regions fill in through proposals."""

from __future__ import annotations

from datetime import date, datetime
from typing import Any

import pytest

from daylog import research, research_flow, services
from daylog.places import PlaceIndex
from daylog.sources import surf
from daylog.vault import Vault
from daylog.webapp import api as webapi


def _hours() -> list[surf.Hour]:
    return [
        surf.Hour(
            time=datetime(2026, 11, 25, h),
            swell_h=1.4,
            swell_t=14.0,
            swell_dir=190.0,
            wind_kn=4.0,
            wind_dir=80.0,
            gust_kn=6.0,
            tide="mid",
            tide_trend="rising",
        )
        for h in range(5, 19)
    ]


@pytest.fixture
def no_network(monkeypatch: pytest.MonkeyPatch) -> list[tuple[float, float]]:
    asked: list[tuple[float, float]] = []

    def fake_fetch(lat: float, lon: float, tz: str, days: int) -> Any:
        asked.append((lat, lon))
        return ("marine", "wind")

    monkeypatch.setattr(surf, "fetch_hourly", fake_fetch)
    monkeypatch.setattr(surf, "parse", lambda m, w: _hours())
    return asked


def _base(
    vault: Vault, place_id: str, lat: float | None = 10.25, lon: float | None = -85.85
) -> None:
    entry: dict[str, Any] = {
        "place": "Guanacaste, Costa Rica",
        "place_id": place_id,
        "from": date(2026, 11, 21),
        "to": None,
    }
    if lat is not None:
        entry |= {"lat": lat, "lon": lon}
    vault.write_yaml("location.yaml", [entry], "base")


def test_a_region_with_no_spots_gets_the_forecast_at_the_base(
    vault: Vault, no_network: Any
) -> None:
    vault.write_place_files(
        {
            "guanacaste": [
                {"id": "costa-rica", "name": "Costa Rica", "kind": "country"},
                {
                    "id": "guanacaste",
                    "name": "Guanacaste",
                    "kind": "region",
                    "parent": "costa-rica",
                },
            ]
        },
        {"guanacaste"},
        "places",
    )
    _base(vault, "guanacaste")
    (spot,) = services.surf_data(vault)
    assert spot["name"] == "Around Guanacaste" and spot["profiled"] is False and spot["days"]
    assert no_network == [(10.25, -85.85)]


def test_unprofiled_spots_get_a_rough_rating_beside_profiled_ones(
    vault: Vault, no_network: Any
) -> None:
    vault.write_place_files(
        {
            "guanacaste": [
                {"id": "guanacaste", "name": "Guanacaste", "kind": "region"},
                {
                    "id": "negra",
                    "name": "Playa Negra",
                    "kind": "surf_spot",
                    "parent": "guanacaste",
                    "lat": 10.20,
                    "lon": -85.84,
                    "surf": {
                        "swell": [180, 300],
                        "best_swell": 235,
                        "offshore": 80,
                        "tide": ["mid", "high"],
                    },
                },
                {
                    "id": "langosta",
                    "name": "Langosta",
                    "kind": "surf_spot",
                    "parent": "guanacaste",
                    "lat": 10.28,
                    "lon": -85.85,
                },
            ]
        },
        {"guanacaste"},
        "places",
    )
    _base(vault, "guanacaste")
    spots = {s["name"]: s for s in services.surf_data(vault)}
    assert spots["Playa Negra"]["profiled"] is True and spots["Langosta"]["profiled"] is False
    assert (
        spots["Playa Negra"]["days"][0]["rating"] > 0 and spots["Langosta"]["days"][0]["rating"] > 0
    )


def test_the_rough_profile_judges_wind_by_strength_only() -> None:
    rough = surf.SurfProfile.rough()
    calm, windy = _hours()[3], _hours()[3]
    windy.wind_kn = 20.0
    assert surf.rate(calm, rough) > surf.rate(windy, rough)
    assert surf.direction_factor(10, rough) == surf.direction_factor(250, rough)


def test_clean_surf_keeps_usable_profiles_only() -> None:
    assert research.clean_surf(
        {"swell": [180, 300], "offshore": 440, "tide": ["mid", "bogus"], "size": [3, 1]}
    ) == {
        "swell": [180.0, 300.0],
        "offshore": 80.0,
        "tide": ["mid"],
    }
    assert research.clean_surf({"swell": [180], "offshore": 80}) is None
    assert research.clean_surf("north") is None


def test_a_places_proposal_becomes_one_card_and_adds_spots_with_profiles(vault: Vault) -> None:
    vault.write_place_files(
        {"guanacaste": [{"id": "guanacaste", "name": "Guanacaste", "kind": "region"}]},
        {"guanacaste"},
        "places",
    )
    vault.write_yaml(
        "research/proposals/2026-11-25-places-guanacaste.yaml",
        {
            "decision": "Surf spots around Guanacaste",
            "made_on": "2026-11-25",
            "by": "routine",
            "places": [
                {
                    "ref": "negra",
                    "name": "Playa Negra",
                    "kind": "surf_spot",
                    "parent_id": "guanacaste",
                    "lat": 10.2016,
                    "lon": -85.8361,
                    "confidence": "verified",
                    "description": "Right reef.",
                    "surf": {"swell": [180, 300], "best_swell": 235, "offshore": 80},
                },
                {
                    "ref": "ghost",
                    "name": "Nowhere",
                    "kind": "surf_spot",
                    "parent_id": "atlantis",
                    "confidence": "inferred",
                    "description": "No parent: dropped.",
                },
            ],
            "status": "pending",
        },
        "proposal",
    )
    app = webapi.Api(vault=lambda: vault, spawn=lambda fn: None, run_notes=lambda fn: fn())
    assert app.waiting_decisions(vault) == 1
    (card,) = [c for c in app.cards.open() if c.kind == "place"]
    assert card.title == "Surf spots around Guanacaste"
    outcome = app.cards.answer(card.id, "all")
    assert outcome is not None and outcome.message == "Added 1 places."
    node = next(n for n in vault.read_places() if n.get("name") == "Playa Negra")
    assert node["surf"]["offshore"] == 80.0 and node["parent"] == "guanacaste"
    assert (
        vault.read_yaml("research/proposals/2026-11-25-places-guanacaste.yaml", {})["status"]
        == "applied"
    )
    assert app.waiting_decisions(vault) == 0
    assert research_flow  # imported for the batch helpers the card uses
    assert PlaceIndex(vault.read_places()).get(node["id"]) is not None
