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


# -- composing pages: extends, templates, weekly times, themes, one-time links -------

BASE = {
    "template": True,
    "title": "Coffee with *Alex*",
    "where": "San Marcos, TX",
    "minutes": 60,
    "theme": "firm",
    "weekly": {"weekdays": ["11:00", "13:00"]},
    "horizon": 7,
}


@pytest.fixture
def composed(vault: Vault) -> Vault:
    vault.write_yaml("booking/base.yaml", BASE, "base")
    vault.write_yaml(
        "booking/sam.yaml",
        {"extends": "base", "guest": "Sam", "theme": "surf", "once": True, "title": "Surf, *Sam*?"},
        "sam",
    )
    vault.write_yaml("booking/kai.yaml", {"extends": "base", "guest": "Kai"}, "kai")
    return vault


def _view(v: Vault, slug: str) -> dict:
    spec = booking.page(v, slug)
    assert spec is not None
    return booking.view(slug, spec, booking.bookings(v), NOW)


def _slots(shown: dict) -> dict[str, dict[str, str]]:
    return {d["date"]: {s["time"]: s["state"] for s in d["slots"]} for d in shown["days"]}


def test_a_page_inherits_from_its_base_and_a_template_isnt_served(composed: Vault) -> None:
    assert booking.page(composed, "base") is None
    shown = _view(composed, "sam")
    assert shown["title"] == "Surf, *Sam*?" and shown["where"] == "San Marcos, TX"
    assert shown["theme"] == "surf" and shown["guest"] == "Sam"
    kai = booking.resolve(composed, "kai") or {}
    # guest/once/template are said about one page only.
    assert kai["guest"] == "Kai" and "template" not in kai and "once" not in kai


def test_weekly_times_over_the_horizon_with_per_date_overrides(vault: Vault) -> None:
    spec = {
        **BASE,
        "template": False,
        "days": [
            {"date": "2026-10-30", "off": True},
            {"date": "2026-11-02", "times": ["15:00"]},
            {"date": "2026-11-03", "held": {"11:00": "at the dentist"}},
            {"date": "2026-11-20", "times": ["09:00"]},
        ],
    }
    vault.write_yaml("booking/w.yaml", spec, "w")
    slots = _slots(_view(vault, "w"))
    # Thu Oct 29 (today) .. Wed Nov 4; the weekend has no weekly times.
    assert list(slots) == ["2026-10-29", "2026-11-02", "2026-11-03", "2026-11-04", "2026-11-20"]
    assert slots["2026-11-02"] == {"15:00": "open"}
    assert slots["2026-11-03"] == {"11:00": "held", "13:00": "open"}


def test_a_slot_booked_on_one_page_is_taken_on_every_page(composed: Vault) -> None:
    booking.request(composed, "kai", {"date": "2026-11-02", "time": "13:00", "name": "Kai"}, NOW)
    assert _slots(_view(composed, "sam"))["2026-11-02"]["13:00"] == "taken"
    assert _slots(_view(composed, "sam"))["2026-11-02"]["11:00"] == "open"


def test_a_one_time_link_fills_then_closes_once_accepted(composed: Vault) -> None:
    saved = booking.request(
        composed, "sam", {"date": "2026-11-02", "time": "11:00", "name": "Sam"}, NOW
    )
    assert all(s != "open" for day in _slots(_view(composed, "sam")).values() for s in day.values())
    with pytest.raises(booking.BookingError):
        booking.request(composed, "sam", {"date": "2026-11-03", "time": "11:00", "name": "X"}, NOW)
    booking.answer(composed, saved["id"], True, NOW.date())
    assert booking.page(composed, "sam") is None
    assert {p["slug"]: p["status"] for p in booking.overview(composed, NOW)}["sam"] == "used"
    # Cancelling frees the slot and the link.
    booking.cancel(composed, saved["id"], NOW.date())
    assert booking.page(composed, "sam") is not None
    assert booking.events(booking.bookings(composed), ZONE) == []


def test_a_page_file_is_checked_before_its_saved(composed: Vault) -> None:
    bad = [
        ({"titel": "x"}, "unknown field"),
        ({"theme": "neon"}, "theme must be one of"),
        ({"weekly": {"someday": ["11:00"]}}, "isn't a day"),
        ({"weekly": {"mon": ["11"]}}, "list of times"),
        ({"extends": "nope"}, "no such page"),
        ({"minutes": 2}, "minutes"),
    ]
    for spec, words in bad:
        with pytest.raises(booking.BookingError) as err:
            booking.check(composed, "x", spec)
        assert words in err.value.message
    with pytest.raises(booking.BookingError, match="loop"):
        booking.update(composed, "base", {"extends": "sam"})
    with pytest.raises(booking.BookingError):
        booking.check(composed, "Bad Slug", {})


def test_update_open_close_and_delete(composed: Vault) -> None:
    booking.update(composed, "kai", {"lede": "Hi Kai", "guest": None})
    own = booking.raw(composed, "kai") or {}
    assert own["lede"] == "Hi Kai" and "guest" not in own
    booking.set_open(composed, "kai", False)
    assert booking.page(composed, "kai") is None
    booking.set_open(composed, "kai", True)
    assert booking.page(composed, "kai") is not None
    with pytest.raises(booking.BookingError, match="extend"):
        booking.delete(composed, "base")
    booking.delete(composed, "kai")
    assert "kai" not in booking.slugs(composed)


def test_a_theme_sets_the_pages_colours_and_fonts(composed: Vault) -> None:
    from daylog import web

    html = web.booking_page(_view(composed, "sam"), "https://book.example.com/sam").decode()
    assert "--accent:#0e6e85" in html and "family=Fraunces" in html and "<!--THEME-->" not in html
    css, fonts = booking.theme_css("firm")
    assert "--accent:#1f5c43" in css and fonts == ""


def test_the_book_domain_root_serves_the_index_page(server: int, page: Vault) -> None:
    page.write_yaml("booking/index.yaml", {"extends": "coffee", "title": "Book a *time*"}, "i")
    status, html = _call(server, "GET", "/", "book.example.com")
    assert status == 200 and "Book a *time*" in html
    body = {"date": "2026-11-02", "time": "11:00", "name": "Someone"}
    assert _call(server, "POST", "/", "book.example.com", body)[0] == 200
    assert booking.bookings(page)[0]["page"] == "index"


# -- the app's Booking screen and chat --------------------------------------------------


def _api(app: webapi.Api, method: str, path: str, body: dict | None = None) -> tuple[int, dict]:
    headers = {"x-daylog": "1", "host": "daylog.example.com"}
    raw = json.dumps(body or {}).encode()
    req = webapi.Request(method=method, path=path, headers=headers, body=raw, signed_in=True)
    return app.handle(req)


def test_the_app_lists_makes_edits_and_deletes_pages(composed: Vault) -> None:
    app = webapi.Api(vault=lambda: composed, now=lambda: NOW)
    status, d = _api(app, "GET", "/api/booking")
    assert status == 200 and d["base"] == "https://book.example.com"
    by = {p["slug"]: p for p in d["pages"]}
    assert by["base"]["status"] == "template" and by["base"]["children"] == ["kai", "sam"]
    assert by["sam"]["path"] == "/sam" and by["sam"]["open_slots"] > 0

    made = _api(app, "POST", "/api/booking/pages", {"slug": "lu", "extends": "base", "once": True})
    assert made[0] == 200
    assert _api(app, "POST", "/api/booking/pages", {"slug": "lu"})[0] == 409

    status, f = _api(app, "GET", "/api/booking/pages/lu")
    assert "extends: base" in f["yaml"] and "weekly" in f["inherited"]
    bad = _api(app, "POST", "/api/booking/pages/lu", {"yaml": "theme: neon\n"})
    assert bad[0] == 400 and "theme" in bad[1]["message"]
    assert _api(app, "POST", "/api/booking/pages/lu", {"yaml": "a: [\n"})[1]["error"] == "bad_yaml"
    ok = _api(app, "POST", "/api/booking/pages/lu", {"yaml": f["yaml"] + "lede: Hey Lu\n"})
    assert ok[0] == 200 and (booking.raw(composed, "lu") or {})["lede"] == "Hey Lu"

    assert _api(app, "POST", "/api/booking/pages/lu/open", {"open": False})[0] == 200
    assert booking.page(composed, "lu") is None
    assert _api(app, "POST", "/api/booking/pages/base/delete")[0] == 409
    assert _api(app, "POST", "/api/booking/pages/lu/delete")[0] == 200


def test_the_app_cancels_a_booking(composed: Vault) -> None:
    saved = booking.request(
        composed, "kai", {"date": "2026-11-02", "time": "11:00", "name": "Kai"}, NOW
    )
    app = webapi.Api(vault=lambda: composed, now=lambda: NOW)
    assert _api(app, "POST", f"/api/booking/{saved['id']}/cancel")[0] == 200
    assert booking.bookings(composed)[0]["status"] == "cancelled"
    assert _api(app, "POST", f"/api/booking/{saved['id']}/cancel")[0] == 409


def test_chat_proposes_a_page_and_the_confirm_writes_it(composed: Vault) -> None:
    from daylog import chat, chat_flow

    tools = chat.Tools(composed, NOW.date())
    assert '"sam"' in tools.run("list_booking_pages", {})
    assert "guest: Sam" in tools.run("get_booking_page", {"slug": "sam"})
    refused = tools.run(
        "propose_booking_page",
        {"action": "create", "slug": "jo", "fields": {"theme": "neon"}, "summary": "x"},
    )
    assert refused.startswith("Can't propose that")
    fields = {"extends": "base", "guest": "Jo", "theme": "sunset", "once": True}
    tools.run(
        "propose_booking_page",
        {"action": "create", "slug": "Jo", "fields": fields, "summary": "Jo's link"},
    )
    (proposal,) = tools.proposals
    assert proposal["slug"] == "jo" and "theme: sunset" in proposal["detail"]
    assert booking.raw(composed, "jo") is None  # nothing until the tap
    chat_flow.apply_edit(composed, proposal, NOW.date())
    assert (booking.raw(composed, "jo") or {})["guest"] == "Jo"
    tools.run("propose_booking_page", {"action": "delete", "slug": "base", "summary": "x"})
    assert len(tools.proposals) == 1  # refused: pages extend it
