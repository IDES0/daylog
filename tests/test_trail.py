from __future__ import annotations

from datetime import date
from typing import Any

from daylog import trail
from daylog.places import PlaceIndex

from .place_fixtures import tree

LOCATION = [
    {
        "place": "Kuta, Lombok, ID",
        "place_id": "kuta",
        "from": date(2026, 9, 14),
        "to": date(2026, 9, 17),
    },
    {"place": "Liveaboard", "mode": "trip", "from": date(2026, 9, 17), "to": date(2026, 9, 20)},
    {"place": "Labuan Bajo, Flores, ID", "from": date(2026, 9, 20), "to": None},
]

JOURNAL: dict[date, dict[str, Any]] = {
    date(2026, 9, 15): {"activities": [{"type": "surf", "place": "ekas-bay"}]},
    date(2026, 9, 16): {
        "activities": [{"type": "surf", "place": "ekas-bay"}, {"type": "walk"}],
        "meals": [{"items": ["ramen"], "place": "ramen-otaku"}],
    },
}


def test_stay_on_treats_end_date_as_the_next_stays_first_day() -> None:
    stays = trail.stays(LOCATION)
    stay = trail.stay_on(stays, date(2026, 9, 17))
    assert stay is not None and stay.mode == "trip"
    stay = trail.stay_on(stays, date(2026, 9, 30))
    assert stay is not None and stay.place.startswith("Labuan Bajo")


def test_day_place_ids_dedupes_across_activities_and_meals() -> None:
    assert trail.day_place_ids(JOURNAL[date(2026, 9, 16)]) == ["ekas-bay", "ramen-otaku"]


def test_visits_rolls_up_to_ancestors_with_an_index() -> None:
    visits = trail.visits(JOURNAL, PlaceIndex(tree()))
    assert visits["ekas-bay"] == [date(2026, 9, 15), date(2026, 9, 16)]
    assert visits["lombok"] == [date(2026, 9, 15), date(2026, 9, 16)]
    assert visits["kuta"] == [date(2026, 9, 16)]


def test_format_trail_groups_by_stay() -> None:
    days = trail.build(LOCATION, JOURNAL, {}, date(2026, 9, 14), date(2026, 9, 21))
    text = trail.format_trail(days, PlaceIndex(tree()))
    assert "Sep 14–Sep 17 · Kuta, Lombok, ID" in text
    assert "  Sep 16: Ekas Bay, Ramen Otaku" in text
    assert "Sep 17–Sep 20 · Liveaboard [trip]" in text
    assert "Sep 20–now · Labuan Bajo, Flores, ID" in text
