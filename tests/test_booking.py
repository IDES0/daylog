from __future__ import annotations

import http.client
import json
import threading
from collections.abc import Iterator
from datetime import datetime
from http.server import ThreadingHTTPServer
from zoneinfo import ZoneInfo

import pytest

from daylog import booking, calendar_feed, calendar_server, push
from daylog.vault import Vault
from daylog.webapp import api as webapi

ZONE = ZoneInfo("America/Chicago")
NOW = datetime(2026, 10, 29, 9, 0, tzinfo=ZONE)

PAGE = {
    "kicker": "Office of Alex B.",
    "title": "Book your *initial consultation*",
    "details": {"Format": "Coffee, in person", "Duration": "45 min"},
    "where": "San Marcos, TX",
    "minutes": 45,
    "event": "Coffee",
    "types": ["Initial consultation", "Strategic coffee alignment"],
    "order_label": "Coffee order",
    "days": [
        {"date": "2026-10-28", "times": ["11:00"]},
        {
            "date": "2026-10-30",
            "times": ["11:00", "12:00"],
            "held": {"13:00": "held for costume procurement."},
        },
        {"date": "2026-11-02", "times": ["11:00", "12:00", "13:00"]},
    ],
}


@pytest.fixture
def page(vault: Vault) -> Vault:
    vault.write_yaml("booking/coffee.yaml", PAGE, "booking page")
    return vault


def _book(v: Vault, **over: str) -> dict:
    data = {
        "date": "2026-11-02",
        "time": "12:00",
        "name": "Charlee",
        "type": "Initial consultation",
    }
    return booking.request(v, "coffee", {**data, **over}, NOW)


def test_the_page_shows_future_days_with_held_and_taken_slots(page: Vault) -> None:
    _book(page)
    shown = booking.view("coffee", booking.page(page, "coffee") or {}, booking.bookings(page), NOW)
    assert [d["date"] for d in shown["days"]] == ["2026-10-30", "2026-11-02"]
    friday = {s["time"]: s["state"] for s in shown["days"][0]["slots"]}
    assert friday == {"11:00": "open", "12:00": "open", "13:00": "held"}
    monday = {s["time"]: s["state"] for s in shown["days"][1]["slots"]}
    assert monday["12:00"] == "taken" and monday["11:00"] == "open"
    assert shown["details"] == [["Format", "Coffee, in person"], ["Duration", "45 min"]]


def test_a_booking_is_saved_pending_and_the_slot_cant_be_taken_twice(page: Vault) -> None:
    saved = _book(page, notes="  vegan  ")
    assert saved["status"] == "pending" and saved["ref"] == "BOOK-1102-1200"
    assert saved["notes"] == "vegan" and saved["minutes"] == 45
    with pytest.raises(booking.BookingError) as taken:
        _book(page, name="Someone else")
    assert taken.value.status == 409


def test_only_offered_slots_and_open_pages_take_bookings(page: Vault) -> None:
    for over in ({"time": "13:00", "date": "2026-10-30"}, {"time": "09:00"}, {"name": " "}):
        with pytest.raises(booking.BookingError):
            _book(page, **over)
    with pytest.raises(booking.BookingError) as missing:
        booking.request(page, "nope", {}, NOW)
    assert missing.value.status == 404
    page.write_yaml("booking/coffee.yaml", {**PAGE, "open": False}, "close")
    assert booking.page(page, "coffee") is None


def test_only_an_accepted_booking_goes_on_the_calendar(page: Vault) -> None:
    first = _book(page)
    second = _book(page, time="11:00")
    assert booking.events(booking.bookings(page), ZONE) == []
    assert booking.answer(page, first["id"], True, NOW.date()) is not None
    assert booking.answer(page, first["id"], False, NOW.date()) is None
    booking.answer(page, second["id"], False, NOW.date())
    events = booking.events(booking.bookings(page), ZONE)
    assert [e["summary"] for e in events] == ["Coffee: Charlee"]
    assert events[0]["start"] == datetime(2026, 11, 2, 12, 0, tzinfo=ZONE)
    feed = calendar_feed.build_feed([], [], [], events).decode()
    assert "SUMMARY:Coffee: Charlee" in feed and "LOCATION:San Marcos\\, TX" in feed
    # A declined slot is open again.
    shown = booking.view("coffee", PAGE, booking.bookings(page), NOW)
    assert {s["time"]: s["state"] for s in shown["days"][1]["slots"]}["11:00"] == "open"


def test_rate_limit_counts_per_client() -> None:
    limit = booking.RateLimit(limit=2, window=60)
    assert limit.allow("a", 0) and limit.allow("a", 1) and not limit.allow("a", 2)
    assert limit.allow("b", 2) and limit.allow("a", 61)


def test_the_app_cards_a_pending_booking_and_accepting_answers_it(page: Vault) -> None:
    saved = _book(page)
    app = webapi.Api(vault=lambda: page, now=lambda: NOW)
    req = webapi.Request(method="GET", path="/api/cards", query={}, headers={}, signed_in=True)
    cards = app.handle(req)[1]["cards"]
    assert [c["kind"] for c in cards] == ["booking"]
    assert cards[0]["title"] == "Charlee booked Monday, Nov 2, 12:00 PM"
    outcome = app.cards.answer(cards[0]["id"], "accept")
    assert outcome is not None and outcome.ok
    assert booking.bookings(page)[0]["status"] == "accepted"
    assert app.handle(req)[1]["cards"] == []
    assert saved["id"] == booking.bookings(page)[0]["id"]


# -- over HTTP ------------------------------------------------------------------


@pytest.fixture
def server(page: Vault, monkeypatch: pytest.MonkeyPatch) -> Iterator[int]:
    monkeypatch.setenv("VAULT_PATH", str(page.path))
    monkeypatch.setattr(calendar_server, "tz", lambda: ZONE)
    monkeypatch.setattr(calendar_server, "datetime", _Frozen)
    monkeypatch.setattr(push, "send", lambda *a, **k: 0)
    handler = calendar_server._make_handler(None, None, True, "pw", webapi.Api(vault=lambda: page))
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    yield httpd.server_address[1]
    httpd.shutdown()


class _Frozen(datetime):
    @classmethod
    def now(cls, tz: object = None) -> _Frozen:  # type: ignore[override]
        return cls(2026, 10, 29, 9, 0, tzinfo=ZONE)


def _call(
    port: int, method: str, path: str, host: str, body: dict | None = None, **headers: str
) -> tuple[int, str]:
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
    sent = {"Host": host, **headers}
    data = None
    if body is not None:
        data = json.dumps(body).encode()
        sent.setdefault("Content-Type", "application/json")
    conn.request(method, path, body=data, headers=sent)
    response = conn.getresponse()
    return response.status, response.read().decode()


def test_the_book_domain_serves_pages_and_nothing_else(server: int) -> None:
    status, html = _call(server, "GET", "/coffee?name=Charlee", "book.example.com")
    assert status == 200 and '"San Marcos, TX"' in html and "__BOOKING__" not in html
    assert _call(server, "GET", "/nope", "book.example.com")[0] == 404
    assert _call(server, "GET", "/", "book.example.com")[0] == 404
    assert _call(server, "GET", "/robots.txt", "book.example.com")[0] == 200
    # The status domain reaches the same page under /book/.
    assert _call(server, "GET", "/book/coffee", "status.example.com")[0] == 200
    assert _call(server, "GET", "/coffee", "status.example.com")[0] == 404


def test_a_page_with_a_preview_image_gets_share_tags(server: int, page: Vault) -> None:
    _, html = _call(server, "GET", "/coffee", "book.example.com")
    assert 'property="og:title" content="Book your initial consultation"' in html
    assert "og:image" not in html
    assert _call(server, "GET", "/coffee.png", "book.example.com")[0] == 404
    page.write_text("booking/coffee.png", "png", "preview")
    _, html = _call(server, "GET", "/coffee?name=Charlee", "book.example.com")
    assert 'content="https://book.example.com/coffee.png"' in html
    assert _call(server, "GET", "/coffee.png", "book.example.com") == (200, "png")


def test_a_guest_books_over_http(server: int, page: Vault) -> None:
    host = "book.example.com"
    body = {"date": "2026-11-02", "time": "12:00", "name": "Charlee"}
    assert _call(server, "POST", "/coffee", host, body, Origin="https://evil.example")[0] == 403
    assert _call(server, "POST", "/coffee", host, {**body, "website": "x"})[0] == 200
    assert booking.bookings(page) == []
    status, out = _call(server, "POST", "/coffee", host, body)
    assert status == 200 and json.loads(out)["ref"] == "BOOK-1102-1200"
    assert _call(server, "POST", "/coffee", host, body)[0] == 409
    assert [b["name"] for b in booking.bookings(page)] == ["Charlee"]
