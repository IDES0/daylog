"""A small places tree shared across tests: two islands that both have a Kuta."""

from __future__ import annotations

from typing import Any


def tree() -> list[dict[str, Any]]:
    return [
        {"id": "indonesia", "name": "Indonesia", "kind": "country", "aliases": ["ID"]},
        {
            "id": "lombok",
            "name": "Lombok",
            "kind": "region",
            "parent": "indonesia",
            "activities": ["surf"],
            "checklist": [{"item": "Dive the Gilis", "status": "todo"}],
        },
        {"id": "kuta", "name": "Kuta", "kind": "town", "parent": "lombok"},
        {
            "id": "ekas-bay",
            "name": "Ekas Bay",
            "kind": "surf_spot",
            "parent": "lombok",
            "lat": -8.9,
            "lon": 116.45,
            "facts": {"break": "right/left"},
        },
        {
            "id": "kuta-wind",
            "name": "Kuta (wind foiling)",
            "kind": "wind_spot",
            "parent": "kuta",
            "lat": -8.8948,
            "lon": 116.2832,
        },
        {"id": "ramen-otaku", "name": "Ramen Otaku", "kind": "food", "parent": "kuta"},
        {"id": "bali", "name": "Bali", "kind": "region", "parent": "indonesia"},
        {"id": "kuta-bali", "name": "Kuta", "kind": "town", "parent": "bali"},
        {
            "id": "airport-rights",
            "name": "Airport Rights",
            "kind": "surf_spot",
            "parent": "kuta-bali",
            "lat": -8.757,
            "lon": 115.16,
        },
        {"id": "flores", "name": "Flores", "kind": "region", "parent": "indonesia"},
        {
            "id": "labuan-bajo",
            "name": "Labuan Bajo",
            "kind": "town",
            "parent": "flores",
            "aliases": ["LBJ"],
        },
    ]
