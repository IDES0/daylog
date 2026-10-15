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


def test_future_map_routes_dated_legs_and_keeps_ideas_apart() -> None:
    itinerary = [
        {
            "place": "Canada",
            "status": "planned",
            "place_id": "canada",
            "target_window": ["2026-12-19", "2026-12-26"],
        },
        {
            "place": "Uluwatu",
            "status": "planned",
            "place_id": "uluwatu",
            "target_window": ["2026-10-05", "2026-10-19"],
        },
        {"place": "Past", "status": "planned", "target_window": ["2026-09-01", "2026-09-05"]},
        {"place": "Done", "status": "done", "place_id": "bali"},
        # A name alone never places a pin.
        {"place": "Bali", "status": "candidate"},
    ]
    location = [{"place": "Bali", "place_id": "bali", "from": "2026-09-20", "to": None}]
    data = dashboard.future_map(itinerary, PlaceIndex(PLACES), location, TODAY)

    assert data["here"] == {"name": "Bali", "lat": -8.4, "lon": 115.2, "country": "Indonesia"}
    assert [(s["name"], s["planned"]) for s in data["stops"]] == [
        ("Uluwatu", True),
        ("Canada", True),
        ("Bali", False),
    ]
    # A spot without coordinates borrows its region's; a country has none.
    assert (data["stops"][0]["lat"], data["stops"][0]["country"]) == (-8.4, "Indonesia")
    assert (data["stops"][1]["lat"], data["stops"][1]["country"]) == (None, "Canada")
    assert data["stops"][2]["lat"] is None and data["stops"][2]["country"] is None


def _seed(vault: Vault) -> None:
    vault.write_yaml(
        "itinerary.yaml",
        [{"place": "</script><b>x", "status": "candidate", "notes": "secret note"}],
        "itinerary",
    )
    vault.write_journal_entry(datetime(2026, 10, 1, 9, 0), {}, "t", "Private summary here")


def test_guest_view_hides_the_private_sections(vault: Vault) -> None:
    _seed(vault)
    html = dashboard.render(vault, TODAY, login=True)

    assert 'name="password"' in html and 'action="/login"' in html
    assert "Private summary" not in html and "secret note" not in html
    # Place names inside the map's JSON can't end the script element.
    assert "</script><b>" not in html and "<\\/script><b>x" in html
    assert 'rel="manifest"' in html


def test_private_view_adds_the_journal_and_a_sign_out(vault: Vault) -> None:
    _seed(vault)
    html = dashboard.render(vault, TODAY, private=True, login=True)

    assert "Private summary here" in html and "secret note" in html
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


@pytest.fixture
def server(vault: Vault, monkeypatch: pytest.MonkeyPatch) -> Iterator[int]:
    monkeypatch.setenv("VAULT_PATH", str(vault.path))
    handler = calendar_server._make_handler(
        None, "/status/alex", None, password="right horse", at_root=True
    )
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    yield httpd.server_address[1]
    httpd.shutdown()


def _request(
    port: int, method: str, path: str, body: str = "", cookie: str = ""
) -> http.client.HTTPResponse:
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
    headers = {"Content-Type": "application/x-www-form-urlencoded"}
    if cookie:
        headers["Cookie"] = cookie
    conn.request(method, path, body=body.encode() if body else None, headers=headers)
    response = conn.getresponse()
    response.read()
    return response


def test_wrong_password_lands_on_the_guest_page(server: int) -> None:
    response = _request(server, "POST", "/login", "password=nope")
    assert response.status == 303 and response.getheader("Location") == "/"
    assert response.getheader("Set-Cookie") is None
    assert _request(server, "GET", "/me").getheader("Location") == "/"


def test_right_password_opens_the_private_page(server: int) -> None:
    response = _request(server, "POST", "/login", "password=right+horse")
    assert response.status == 303 and response.getheader("Location") == "/me"
    cookie = (response.getheader("Set-Cookie") or "").split(";")[0]
    assert cookie.startswith(web.SESSION_COOKIE + "=")

    assert _request(server, "GET", "/me", cookie=cookie).status == 200
    # Signed in, the guest page sends you on to your own.
    assert _request(server, "GET", "/", cookie=cookie).getheader("Location") == "/me"
    signed_out = _request(server, "POST", "/logout", cookie=cookie)
    assert "Max-Age=0" in (signed_out.getheader("Set-Cookie") or "")


def test_guest_page_and_app_files_are_served(server: int) -> None:
    for path in ("/", "/status/alex", "/manifest.webmanifest", "/sw.js", "/icon-180.png"):
        assert _request(server, "GET", path).status == 200, path
    assert _request(server, "GET", "/status/other").status == 404


def test_page_under_a_secret_is_not_served_at_the_root(
    vault: Vault, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("VAULT_PATH", str(vault.path))
    handler = calendar_server._make_handler(None, "/status/s3cret", None, password="pw")
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    try:
        port = httpd.server_address[1]
        assert _request(port, "GET", "/").status == 404
        refused = _request(port, "POST", "/login", "password=x")
        assert refused.getheader("Location") == "/status/s3cret"
    finally:
        httpd.shutdown()
