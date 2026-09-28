from __future__ import annotations

from daylog.places import PlaceIndex, file_for, new_node_id, outline

from .place_fixtures import tree


def _index() -> PlaceIndex:
    return PlaceIndex(tree())


def test_path_name_walks_the_parent_chain() -> None:
    assert _index().path_name("airport-rights") == "Airport Rights, Kuta, Bali, Indonesia"


def test_region_of_is_the_nearest_region_above() -> None:
    index = _index()
    region = index.region_of("kuta-wind")
    assert region is not None and region["id"] == "lombok"
    region = index.region_of("airport-rights")
    assert region is not None and region["id"] == "bali"


def test_resolve_prefers_the_match_inside_the_given_area() -> None:
    index = _index()
    assert index.resolve("kuta", within="bali")[0] == "kuta-bali"
    assert index.resolve("kuta", within="lombok")[0] == "kuta"


def test_resolve_matches_aliases_case_insensitively() -> None:
    assert _index().resolve("lbj") == ["labuan-bajo"]


def test_resolve_location_uses_the_region_part_to_disambiguate() -> None:
    index = _index()
    node = index.resolve_location("Kuta, Lombok, ID")
    assert node is not None and node["id"] == "kuta"
    node = index.resolve_location("Kuta, Bali, ID")
    assert node is not None and node["id"] == "kuta-bali"


def test_resolve_location_never_matches_a_bare_country() -> None:
    # "Komodo, Indonesia" used to match whichever Indonesian region came first.
    assert _index().resolve_location("Komodo, Indonesia") is None


def test_current_place_prefers_an_explicit_place_id() -> None:
    node = _index().current_place({"place": "Kuta, Bali, ID", "place_id": "kuta"})
    assert node is not None and node["id"] == "kuta"


def test_forecast_spots_covers_the_whole_current_region() -> None:
    index = _index()
    spots = index.forecast_spots(index.by_id["kuta"])
    assert {s["id"] for s in spots} == {"ekas-bay", "kuta-wind"}


def test_new_node_id_appends_region_on_collision() -> None:
    index = _index()
    assert new_node_id(index, "Gerupuk", "lombok") == "gerupuk"
    assert new_node_id(index, "Ekas Bay", "bali") == "ekas-bay-bali"


def test_file_for_is_the_region_or_world() -> None:
    index = _index()
    assert file_for(index, "kuta") == "lombok"
    assert file_for(index, "indonesia") == "_world"
    assert file_for(index, None) == "_world"


def test_outline_hides_food_outside_the_focus_region() -> None:
    index = _index()
    focused = outline(index, index.by_id["kuta"])
    assert "ramen-otaku" in focused
    elsewhere = outline(index, index.by_id["labuan-bajo"])
    assert "ramen-otaku" not in elsewhere
    assert "airport-rights: Airport Rights (surf_spot)" in elsewhere


def test_ancestors_survive_a_cycle() -> None:
    nodes = [
        {"id": "a", "name": "A", "kind": "town", "parent": "b"},
        {"id": "b", "name": "B", "kind": "town", "parent": "a"},
    ]
    assert [n["id"] for n in PlaceIndex(nodes).ancestors("a")] == ["b"]


def test_with_coordinates_borrows_the_nearest_ancestors() -> None:
    index = _index()
    nodes = index.with_coordinates([index.by_id["ramen-otaku"], index.by_id["ekas-bay"]])
    # ramen-otaku has no coords and neither do kuta/lombok/indonesia in the fixture → dropped
    assert [n["id"] for n in nodes] == ["ekas-bay"]
    index.by_id["kuta"]["lat"], index.by_id["kuta"]["lon"] = -8.9, 116.3
    nodes = index.with_coordinates([index.by_id["ramen-otaku"]])
    assert nodes[0]["lat"] == -8.9 and "lat" not in index.by_id["ramen-otaku"]
