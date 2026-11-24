"""Places visited from the phone's location (Overland / OwnTracks), invented data."""

from __future__ import annotations

import http.client
import json
import threading
from datetime import UTC, date, datetime, timedelta
from http.server import ThreadingHTTPServer
from typing import Any
from zoneinfo import ZoneInfo

import pytest

from daylog import calendar_feed, calendar_server, location_feed, timeline
from daylog.places import PlaceIndex
from daylog.vault import Vault

TZ = ZoneInfo("America/Chicago")
HOME = (30.2672, -97.7431)
WAVE_POOL = (31.5493, -97.1467)


def _index() -> PlaceIndex:
    return PlaceIndex(
        [
            {"id": "austin", "name": "Austin", "kind": "town", "lat": HOME[0], "lon": HOME[1]},
            {
                "id": "waco-surf",
                "name": "Waco Surf",
                "kind": "wave_pool",
                "lat": WAVE_POOL[0],
                "lon": WAVE_POOL[1],
            },
        ]
    )


def _overland(points: list[tuple[str, tuple[float, float]]], acc: float = 20) -> dict[str, Any]:
    return {
        "locations": [
            {
                "type": "Feature",
                "geometry": {"type": "Point", "coordinates": [lon, lat]},
                "properties": {"timestamp": ts, "horizontal_accuracy": acc, "motion": []},
            }
            for ts, (lat, lon) in points
        ]
    }


def _near(p: tuple[float, float], metres: float) -> tuple[float, float]:
    return (p[0] + metres / 111_000, p[1])


def test_parse_reads_both_apps_and_drops_vague_fixes() -> None:
    overland = location_feed.parse(
        _overland([("2026-11-23T15:40:00-06:00", WAVE_POOL)])
        | {"locations": _overland([("2026-11-23T15:41:00Z", WAVE_POOL)], acc=900)["locations"]}
    )
    assert len(overland) == 0  # the second batch replaced the first and was too vague
    (p,) = location_feed.parse(_overland([("2026-11-23T15:40:00-06:00", WAVE_POOL)]))
    assert p.at == datetime(2026, 11, 23, 21, 40, tzinfo=UTC) and p.lat == WAVE_POOL[0]
    owntracks = location_feed.parse(
        [
            {"_type": "location", "lat": HOME[0], "lon": HOME[1], "tst": 1_763_900_000, "acc": 15},
            {"_type": "transition", "lat": 0, "lon": 0, "tst": 1},
        ]
    )
    assert len(owntracks) == 1 and owntracks[0].lon == HOME[1]
    assert location_feed.parse({"nothing": True}) == []


def test_stays_need_ten_minutes_and_the_last_one_stays_open() -> None:
    t0 = datetime(2026, 11, 23, 21, 0, tzinfo=UTC)
    pts = [
        location_feed.Point(t0 + timedelta(minutes=m), *pos)
        for m, pos in [
            (0, HOME),
            (20, _near(HOME, 40)),
            (45, _near(HOME, 60)),  # home 45 min
            (60, (30.5, -97.5)),  # a pass-through, not a stay
            (120, WAVE_POOL),
            (200, _near(WAVE_POOL, 50)),  # the wave pool, still there
        ]
    ]
    closed, still_open = location_feed.stays([], pts)
    assert len(closed) == 1
    start, end, lat, lon = closed[0]
    assert (start, end) == (t0, t0 + timedelta(minutes=45))
    assert location_feed.distance_m(lat, lon, *HOME) < 50
    assert [p.lat for p in still_open][0] == WAVE_POOL[0]
    # A re-sent point changes nothing.
    again, open2 = location_feed.stays(still_open, pts[-1:])
    assert again == [] and len(open2) == len(still_open)


def test_visits_split_at_midnight_and_name_known_places() -> None:
    start = datetime(2026, 11, 24, 3, 0, tzinfo=UTC)  # 21:00 local
    end = datetime(2026, 11, 24, 14, 0, tzinfo=UTC)  # 08:00 local next day
    by_day = location_feed.visits_by_day([(start, end, *WAVE_POOL)], _index(), TZ)
    first, second = by_day[date(2026, 11, 23)], by_day[date(2026, 11, 24)]
    assert first[0]["start"] == "2026-11-23 21:00:00 -0600"
    assert first[0]["end"] == "2026-11-23 23:59:59 -0600"
    assert second[0]["end"] == "2026-11-24 08:00:00 -0600"
    assert first[0]["name"] == "Waco Surf" and first[0]["place"] == "waco-surf"
    unnamed = location_feed.visits_by_day([(start, end, 10.0, 10.0)], _index(), TZ)
    assert "name" not in unnamed[date(2026, 11, 23)][0]  # a town isn't "here"


def test_ingest_keeps_raw_points_off_git_and_writes_visits(vault: Vault) -> None:
    first = _overland(
        [
            ("2026-11-23T15:40:00-06:00", WAVE_POOL),
            ("2026-11-23T17:00:00-06:00", _near(WAVE_POOL, 30)),
        ]
    )
    line, reply, _ = location_feed.ingest(vault, json.dumps(first).encode(), _index(), TZ)
    assert reply == {"result": "ok"} and line == "2 points, 0 visit(s) closed"
    assert location_feed.read_days(vault, date(2026, 11, 23), date(2026, 11, 23)) == {}
    cur = location_feed.current(vault)
    assert cur is not None and abs(cur[0] - WAVE_POOL[0]) < 0.001
    second = _overland(
        [("2026-11-23T19:10:00-06:00", _near(WAVE_POOL, 20)), ("2026-11-23T21:00:00-06:00", HOME)]
    )
    line, _, closed = location_feed.ingest(vault, json.dumps(second).encode(), _index(), TZ)
    assert line == "2 points, 1 visit(s) closed"
    (visit,) = location_feed.read_days(vault, date(2026, 11, 23), date(2026, 11, 23))[
        date(2026, 11, 23)
    ]
    assert (visit["start"], visit["end"], visit["name"]) == (
        "2026-11-23 15:40:00 -0600",
        "2026-11-23 19:10:00 -0600",
        "Waco Surf",
    )
    tracked = vault._run_git("ls-files").stdout
    assert "location/days/2026-11-23.yaml" in tracked and "location_stay" not in tracked
    assert location_feed.ingest(vault, b'[{"_type": "location"}]', _index(), TZ)[1] == []
    with pytest.raises(location_feed.LocationError):
        location_feed.ingest(vault, b"nope", _index(), TZ)


def test_visits_on_the_calendar_and_in_the_feed() -> None:
    visits: list[dict[str, Any]] = [
        {
            "start": "2026-11-23 15:40:00 -0600",
            "end": "2026-11-23 19:10:00 -0600",
            "name": "Waco Surf",
            "place": "waco-surf",
        },
        {
            "start": "2026-11-23 20:00:00 -0600",
            "end": "2026-11-23 20:20:00 -0600",
            "lat": 30.1,
            "lon": -97.1,
        },
    ]
    day = date(2026, 11, 23)
    blocks = [
        b
        for b in timeline.day_blocks({}, None, None, None, day, None, visits)
        if b.layer == "place"
    ]
    assert [(b.detail, b.start, b.end) for b in blocks] == [
        ("Waco Surf", 15 + 40 / 60, 19 + 10 / 60),
        ("", 20.0, 20 + 20 / 60),
    ]
    events = calendar_feed.block_events(day, blocks, TZ)
    assert [e["summary"] for e in events] == ["📍 Waco Surf"]  # the short unnamed stop stays off


def test_the_http_route_answers_overland(vault: Vault, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("VAULT_PATH", str(vault.path))
    handler = calendar_server._make_handler(None, "/health/s3cret")
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    try:
        body = json.dumps(_overland([("2026-11-23T15:40:00-06:00", WAVE_POOL)])).encode()
        conn = http.client.HTTPConnection("127.0.0.1", httpd.server_address[1], timeout=5)
        conn.request(
            "POST", "/location/s3cret", body=body, headers={"Content-Type": "application/json"}
        )
        r = conn.getresponse()
        assert r.status == 200 and json.loads(r.read()) == {"result": "ok"}
        conn = http.client.HTTPConnection("127.0.0.1", httpd.server_address[1], timeout=5)
        conn.request("POST", "/location/wrong", body=body)
        assert conn.getresponse().status == 404
    finally:
        httpd.shutdown()


def test_a_stay_takes_its_name_from_the_note_and_trips_show_between() -> None:
    day = date(2026, 11, 21)
    fm = {
        "meals": [{"items": ["paella"], "place": "bolas", "when": "dinner"}],
        "activities": [{"type": "surf", "place_mention": "Waco Surf", "at": "15:00", "hours": 2}],
    }
    visits: list[dict[str, Any]] = [
        {
            "start": "2026-11-21 15:00:00 -0600",
            "end": "2026-11-21 17:10:00 -0600",
            "lat": WAVE_POOL[0],
            "lon": WAVE_POOL[1],
        },
        {
            "start": "2026-11-21 19:30:00 -0600",
            "end": "2026-11-21 20:45:00 -0600",
            "lat": HOME[0],
            "lon": HOME[1],
        },
    ]
    names = {"bolas": "Bola's Restaurant"}
    blocks = [
        b
        for b in timeline.day_blocks(fm, None, None, None, day, None, visits, names)
        if b.layer == "place"
    ]
    surf, trip, dinner = blocks
    assert (surf.detail, surf.facts["guess"]) == ("Waco Surf", True)
    assert (dinner.detail, dinner.facts["noted_place"]) == ("Bola's Restaurant", "bolas")
    assert (trip.kind, trip.detail, trip.facts["minutes"]) == (
        "travel",
        "to Bola's Restaurant",
        140,
    )
    assert trip.facts["km"] > 100
    events = calendar_feed.block_events(day, blocks, TZ)
    assert [e["summary"] for e in events] == ["📍 Waco Surf?", "📍 Bola's Restaurant?"]


def test_a_matched_stay_offers_to_save_the_places_location(vault: Vault) -> None:
    from daylog.webapp import api as webapi

    day = date(2026, 11, 21)
    vault.write_place_files(
        {
            "texas": [
                {"id": "austin", "name": "Austin", "kind": "town"},
                {"id": "bolas", "name": "Bola's Restaurant", "kind": "food", "parent": "austin"},
            ]
        },
        {"texas"},
        "places",
    )
    vault.write_journal_entry(
        datetime(2026, 11, 21, 21, 0),
        {"meals": [{"items": ["paella"], "place": "bolas", "when": "dinner"}]},
        "t",
        "s",
    )
    visit = {
        "start": "2026-11-21 19:30:00 -0600",
        "end": "2026-11-21 20:45:00 -0600",
        "lat": 30.2601,
        "lon": -97.7502,
    }
    vault.write_yaml(location_feed.day_file(day), {"visits": [visit]}, "visits")
    app = webapi.Api(vault=lambda: vault, spawn=lambda fn: None, run_notes=lambda fn: fn())
    (card,) = app.visit_cards({day: [visit]})
    assert card.title == "Save where Bola's Restaurant is?"
    assert app.visit_cards({day: [visit]}) == []  # offered once
    outcome = app.cards.answer(card.id, "save")
    assert outcome is not None and outcome.message.startswith("Saved where Bola's Restaurant is")
    node = PlaceIndex(vault.read_places()).get("bolas")
    assert node is not None and (node["lat"], node["lon"], node["confidence"]) == (
        30.2601,
        -97.7502,
        "approximate",
    )
    (named,) = location_feed.read_days(vault, day, day)[day]
    assert named["name"] == "Bola's Restaurant" and named["place"] == "bolas"
    # Two stays in the dinner window: ambiguous, no card.
    other = dict(visit, start="2026-11-21 21:00:00 -0600", end="2026-11-21 22:30:00 -0600")
    app2 = webapi.Api(vault=lambda: vault, spawn=lambda fn: None, run_notes=lambda fn: fn())
    vault.write_place_files(
        {
            "texas": [
                {"id": "austin", "name": "Austin", "kind": "town"},
                {"id": "bolas", "name": "Bola's Restaurant", "kind": "food", "parent": "austin"},
            ]
        },
        {"texas"},
        "places: forget the location",
    )
    assert app2.visit_cards({day: [visit, other]}) == []
