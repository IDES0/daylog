from __future__ import annotations

import http.client
import json
import threading
from collections.abc import Iterator
from datetime import date, datetime
from http.server import ThreadingHTTPServer

import pytest

from daylog import calendar_server, dashboard, web
from daylog.places import PlaceIndex
from daylog.vault import Vault

TODAY = date(2026, 10, 2)
PLACES = [
    {"id": "indonesia", "name": "Indonesia", "kind": "country"},
    {"id": "japan", "name": "Japan", "kind": "country"},
    {
        "id": "bali",
        "name": "Bali",
        "kind": "region",
        "parent": "indonesia",
        "lat": -8.4,
        "lon": 115.2,
    },
    {"id": "uluwatu", "name": "Uluwatu", "kind": "surf_spot", "parent": "bali"},
    {"id": "canada", "name": "Canada", "kind": "country"},
]


def test_future_map_shows_only_the_legs_ahead_in_order() -> None:
    itinerary = [
        {
            "place": "Canada — Christmas",
            "status": "planned",
            "place_id": "canada",
            "target_window": ["2026-12-19", "2026-12-26"],
        },
        # No place_id, but the one country named in it.
        {"place": "Flight to Japan", "status": "planned", "deadline": "2026-12-27"},
        # No place_id, but exactly one place by that name.
        {"place": "Uluwatu", "status": "planned", "target_window": ["2026-10-05", "2026-10-19"]},
        # Under way: where you are, not where you're headed.
        {"place": "Bali", "status": "planned", "target_window": ["2026-09-28", "2026-10-04"]},
        {"place": "Past", "status": "planned", "target_window": ["2026-09-01", "2026-09-05"]},
        {"place": "Done", "status": "done", "place_id": "bali", "deadline": "2026-11-01"},
        # Undated: the wishlist, not the plan.
        {"place": "Bali", "status": "candidate", "place_id": "bali"},
    ]
    location = [{"place": "Bali", "place_id": "bali", "from": "2026-09-20", "to": None}]
    data = dashboard.future_map(itinerary, PlaceIndex(PLACES), location, TODAY)

    assert data["here"] == {"name": "Bali", "lat": -8.4, "lon": 115.2, "country": "Indonesia"}
    assert [(s["name"], s["when"]) for s in data["stops"]] == [
        ("Uluwatu", "Oct 5 – Oct 19"),
        ("Canada — Christmas", "Dec 19 – Dec 26"),
        ("Flight to Japan", "by Dec 27"),
    ]
    # A spot without coordinates borrows its region's; a country is placed by the map.
    assert (data["stops"][0]["lat"], data["stops"][0]["country"]) == (-8.4, "Indonesia")
    assert (data["stops"][1]["lat"], data["stops"][1]["country"]) == (None, "Canada")
    assert (data["stops"][2]["lat"], data["stops"][2]["country"]) == (None, "Japan")


def test_a_name_that_could_mean_two_places_places_nothing() -> None:
    index = PlaceIndex(
        [
            *PLACES,
            {"id": "kuta-l", "name": "Kuta", "kind": "town"},
            {"id": "kuta-b", "name": "Kuta", "kind": "town"},
        ]
    )
    itinerary = [{"place": "Kuta", "status": "planned", "deadline": "2026-11-01"}]
    stop = dashboard.future_map(itinerary, index, [], TODAY)["stops"][0]
    assert (stop["lat"], stop["country"]) == (None, None)


def _seed(vault: Vault) -> None:
    vault.write_yaml(
        "itinerary.yaml",
        [{"place": "</script><b>x", "status": "planned", "deadline": "2026-11-01"}],
        "itinerary",
    )
    vault.write_journal_entry(datetime(2026, 10, 1, 9, 0), {}, "t", "Private summary here")


def test_guest_view_hides_the_private_sections(vault: Vault) -> None:
    _seed(vault)
    html = dashboard.render(vault, TODAY, login=True)

    assert 'name="password"' in html and 'action="/login"' in html
    assert "Private summary" not in html
    # Place names inside the map's JSON can't end the script element.
    assert "</script><b>" not in html and "<\\/script><b>x" in html
    assert 'rel="manifest"' in html


def test_private_view_adds_the_journal_and_a_sign_out(vault: Vault) -> None:
    _seed(vault)
    html = dashboard.render(vault, TODAY, private=True, login=True)

    assert "Private summary here" in html
    assert 'action="/logout"' in html and 'name="password"' not in html


def test_session_tokens_expire_and_follow_the_password() -> None:
    token = web.make_session("pw", now=1000)
    assert web.valid_session(token, "pw", now=2000)
    assert not web.valid_session(token, "other", now=2000)
    assert not web.valid_session(token, "pw", now=1000 + web.SESSION_DAYS * 86400 + 1)
    assert not web.valid_session("123.abc", "pw", now=0)
    assert not web.valid_session(token, None, now=2000)
    assert web.session_from(f"a=1; {web.SESSION_COOKIE}={token}") == token


def test_login_guard_stops_answering_after_too_many_misses() -> None:
    guard = web.LoginGuard("pw", max_failures=2, window_s=60)
    assert not guard.attempt("x", now=0)
    assert guard.attempt("pw", now=1)
    assert not guard.attempt("y", now=2)
    # Locked: even the right password fails until the window passes.
    assert not guard.attempt("pw", now=3)
    assert guard.attempt("pw", now=61)


def test_icons_are_pngs_of_the_right_size() -> None:
    png = web.icon_png(32)
    assert png.startswith(b"\x89PNG\r\n\x1a\n")
    assert int.from_bytes(png[16:20], "big") == 32
    assert json.loads(web.manifest())["start_url"] == "/"


def _serve(handler: type) -> tuple[ThreadingHTTPServer, int]:
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    return httpd, httpd.server_address[1]


@pytest.fixture
def server(vault: Vault, monkeypatch: pytest.MonkeyPatch) -> Iterator[int]:
    monkeypatch.setenv("VAULT_PATH", str(vault.path))
    vault.write_journal_entry(datetime(2026, 10, 1, 9, 0), {}, "t", "Private summary here")
    httpd, port = _serve(calendar_server._make_handler(None, None, True, "right horse"))
    yield port
    httpd.shutdown()


def _request(
    port: int, method: str, path: str, body: str = "", cookie: str = ""
) -> tuple[http.client.HTTPResponse, str]:
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
    headers = {"Content-Type": "application/x-www-form-urlencoded"}
    if cookie:
        headers["Cookie"] = cookie
    conn.request(method, path, body=body.encode() if body else None, headers=headers)
    response = conn.getresponse()
    return response, response.read().decode("utf-8", errors="replace")


def test_wrong_password_lands_on_the_guest_view(server: int) -> None:
    response, _ = _request(server, "POST", "/login", "password=nope")
    assert response.status == 303 and response.getheader("Location") == "/"
    assert response.getheader("Set-Cookie") is None
    _, page = _request(server, "GET", "/")
    assert 'name="password"' in page and 'action="/logout"' not in page


def test_right_password_shows_the_private_view_at_the_same_address(server: int) -> None:
    response, _ = _request(server, "POST", "/login", "password=right+horse")
    assert response.status == 303 and response.getheader("Location") == "/"
    cookie = (response.getheader("Set-Cookie") or "").split(";")[0]
    assert cookie.startswith(web.SESSION_COOKIE + "=")

    page_response, page = _request(server, "GET", "/", cookie=cookie)
    assert page_response.status == 200 and 'action="/logout"' in page
    signed_out, _ = _request(server, "POST", "/logout", cookie=cookie)
    assert "Max-Age=0" in (signed_out.getheader("Set-Cookie") or "")


def test_only_the_root_and_app_files_are_served(server: int) -> None:
    for path in ("/", "/manifest.webmanifest", "/sw.js", "/icon-180.png"):
        assert _request(server, "GET", path)[0].status == 200, path
    for path in ("/me", "/status/alex"):
        assert _request(server, "GET", path)[0].status == 404, path


def test_site_off_serves_nothing_at_the_root(vault: Vault, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("VAULT_PATH", str(vault.path))
    httpd, port = _serve(calendar_server._make_handler(None, None))
    try:
        assert _request(port, "GET", "/")[0].status == 404
        assert _request(port, "POST", "/login", "password=x")[0].status == 404
    finally:
        httpd.shutdown()


def test_private_view_has_the_week_calendar_with_each_days_summary(vault: Vault) -> None:
    vault.write_journal_entry(
        datetime(2026, 10, 1, 9, 0),
        {"activities": [{"type": "surf", "at": "06:00", "hours": 2}, {"type": "work"}]},
        "t",
        "Dawn surf <then> work.",
    )
    html = dashboard.render(vault, TODAY, private=True)

    assert '<template id="day-2026-10-01">' in html and "Dawn surf &lt;then&gt; work." in html
    assert 'class="blk"' in html and 'class="blk guess"' in html
    assert '<dialog id="dayDialog">' in html
    # Sep 28 – Oct 4: an earlier week to go back to, no later one yet.
    assert 'href="/?week=2026-09-21"' in html and "Next week" not in html
    assert "Sep 28 – Oct 4" in html
    # The guest view has no calendar.
    assert "dayDialog" not in dashboard.render(vault, TODAY)


def test_week_query_picks_the_calendar_week_when_signed_in(server: int) -> None:
    response, _ = _request(server, "POST", "/login", "password=right+horse")
    cookie = (response.getheader("Set-Cookie") or "").split(";")[0]
    _, page = _request(server, "GET", "/?week=2026-10-01", cookie=cookie)
    assert '<template id="day-2026-10-01">' in page and "Private summary here" in page
    _, bad = _request(server, "GET", "/?week=nope", cookie=cookie)
    assert '<dialog id="dayDialog">' in bad
