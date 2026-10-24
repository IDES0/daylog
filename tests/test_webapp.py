from __future__ import annotations

import http.client
import json
import threading
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import date, datetime
from http.server import ThreadingHTTPServer
from typing import Any

import pytest

from daylog import calendar_server, chat, daily, extract, journal_service, web
from daylog.runtime import tz
from daylog.vault import Vault
from daylog.webapp import api as webapi
from daylog.webapp.cards import CardStore, Outcome, UnknownAction

PASSWORD = "right horse"


@dataclass
class App:
    port: int
    api: webapi.Api
    cookie: str


@pytest.fixture
def app(vault: Vault, monkeypatch: pytest.MonkeyPatch) -> Iterator[App]:
    monkeypatch.setenv("VAULT_PATH", str(vault.path))
    # No background research threads (they'd call the API) in tests.
    # Notes are logged as they arrive here, so a job is done when the POST returns.
    api = webapi.Api(vault=lambda: vault, spawn=lambda fn: None, run_notes=lambda fn: fn())
    handler = calendar_server._make_handler(None, None, True, PASSWORD, app=api)
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    cookie = f"{web.SESSION_COOKIE}={web.make_session(PASSWORD)}"
    yield App(httpd.server_address[1], api, cookie)
    httpd.shutdown()


def _call(
    app: App,
    method: str,
    path: str,
    body: Any = None,
    *,
    signed_in: bool = True,
    csrf: bool = True,
    headers: dict[str, str] | None = None,
) -> tuple[http.client.HTTPResponse, bytes]:
    conn = http.client.HTTPConnection("127.0.0.1", app.port, timeout=10)
    sent = dict(headers or {})
    if signed_in:
        sent["Cookie"] = app.cookie
    if csrf and method != "GET":
        sent["X-Daylog"] = "1"
    data = None
    if body is not None:
        data = body if isinstance(body, bytes) else json.dumps(body).encode()
        sent.setdefault("Content-Type", "application/json")
    conn.request(method, path, body=data, headers=sent)
    response = conn.getresponse()
    return response, response.read()


def _json(app: App, method: str, path: str, body: Any = None, **kw: Any) -> tuple[int, Any]:
    response, raw = _call(app, method, path, body, **kw)
    assert response.getheader("Content-Type") == "application/json; charset=utf-8"
    assert response.getheader("Cache-Control") == "no-store"
    assert response.getheader("Access-Control-Allow-Origin") is None
    return response.status, json.loads(raw)


# -- the rules every endpoint follows -------------------------------------------------


def test_signed_out_gets_401_before_anything_else(app: App) -> None:
    for method, path in (("GET", "/api/home"), ("POST", "/api/log"), ("GET", "/api/nope")):
        status, body = _json(app, method, path, {"text": "x"}, signed_in=False)
        assert (status, body) == (401, {"error": "signed_out"}), path


def test_writes_need_the_csrf_header_and_a_matching_origin(app: App) -> None:
    status, _ = _json(app, "POST", "/api/chat/reset", {}, csrf=False)
    assert status == 403
    status, _ = _json(
        app, "POST", "/api/chat/reset", {}, headers={"Origin": "https://evil.example"}
    )
    assert status == 403
    status, body = _json(
        app, "POST", "/api/chat/reset", {}, headers={"Origin": f"http://127.0.0.1:{app.port}"}
    )
    assert (status, body) == (200, {"ok": True})
    # Reads don't need the header.
    assert _json(app, "GET", "/api/me", csrf=False)[0] == 200


def test_body_limits_and_bad_json(app: App) -> None:
    status, body = _json(app, "POST", "/api/log", b"x" * (webapi.MAX_JSON_BYTES + 1))
    assert (status, body["error"]) == (413, "too_large")
    status, body = _json(app, "POST", "/api/log", b"{nope")
    assert (status, body["error"]) == (400, "bad_json")
    assert _json(app, "GET", "/api/nothing-here")[0] == 404


def test_csrf_rule_unit() -> None:
    assert webapi.csrf_ok("GET", {})
    assert not webapi.csrf_ok("POST", {})
    assert webapi.csrf_ok("POST", {"x-daylog": "1"})
    assert webapi.csrf_ok("POST", {"x-daylog": "1", "origin": "https://a.com", "host": "a.com"})
    assert not webapi.csrf_ok("POST", {"x-daylog": "1", "origin": "null", "host": "a.com"})


# -- reading ---------------------------------------------------------------------------


def _seed(vault: Vault) -> None:
    vault.write_yaml(
        "goals.yaml",
        [
            {
                "id": "cert",
                "title": "Instructor cert",
                "type": "hard",
                "deadline": date(2099, 1, 1),
                "status": "active",
            },
            {
                "id": "surf",
                "title": "Surf more",
                "type": "soft",
                "metric": "sessions",
                "progress": 4,
                "target": 10,
                "status": "active",
            },
        ],
        "goals",
    )
    vault.write_disciplines(
        {
            "water": {"name": "Water"},
            "surf": {"name": "Surf", "parents": ["water"], "status": "active"},
        },
        "disciplines",
    )
    vault.write_journal_entry(
        datetime(2026, 10, 1, 9, 0),
        {"activities": [{"type": "surf", "hours": 2, "at": "06:00"}]},
        "Surfed at dawn.",
        "Dawn surf.",
    )


def test_home_disciplines_and_journal(app: App, vault: Vault) -> None:
    _seed(vault)
    status, home = _json(app, "GET", "/api/home")
    assert status == 200
    assert home["today"] == daily.logical_day(datetime.now(tz())).isoformat()
    surf = next(g for g in home["goals"] if g["id"] == "surf")
    assert (surf["progress"], surf["target"], surf["percent"]) == (4, 10, 40)
    assert home["open_cards"] == 0

    status, graph = _json(app, "GET", "/api/disciplines?days=30")
    assert status == 200 and graph["days"] == 30
    assert {n["id"] for n in graph["nodes"]} == {"water", "surf"}
    assert graph["slots"]["water"] == 1

    status, journal = _json(app, "GET", "/api/journal?from=2026-09-25&to=2026-10-02")
    assert status == 200
    [day] = journal["days"]
    assert day["date"] == "2026-10-01" and day["summary"].endswith("Dawn surf.")
    assert day["activities"][0]["type"] == "surf" and day["notes"] == 1
    assert "transcript" not in day

    status, one = _json(app, "GET", "/api/journal/2026-10-01")
    assert status == 200 and "Surfed at dawn." in one["transcript"]
    assert _json(app, "GET", "/api/journal/2026-01-01")[0] == 404

    status, cal = _json(app, "GET", "/api/calendar?week=2026-10-01")
    assert cal["week_of"] == "2026-09-28" and len(cal["days"]) == 7
    block = cal["days"][3]["blocks"][0]
    assert (block["label"], block["start"], block["hours"]) == ("surf", 6.0, 2.0)


# -- writing ------------------------------------------------------------------------------


def test_a_note_logs_and_a_hard_slip_waits_for_its_card(
    app: App, vault: Vault, monkeypatch: pytest.MonkeyPatch
) -> None:
    _seed(vault)

    def fake_extract(transcript: str, *args: Any, **kwargs: Any) -> dict[str, Any]:
        assert kwargs["recorded_at"] is None  # a picked day is a backdated note
        return {
            "summary": "Surfed and pushed the cert.",
            "activities": [{"type": "surf", "hours": 1}],
            "goal_slips": [{"goal_id": "cert", "new_date": "2099-02-01", "reason": "course full"}],
        }

    monkeypatch.setattr(extract, "extract", fake_extract)
    body = _logged(app, {"text": "surfed", "date": "2026-10-01"})
    assert body["date"] == "2026-10-01" and body["summary"] == "Surfed and pushed the cert."
    [card] = body["cards"]
    assert card["kind"] == "goal_slip" and "hard deadline" in card["title"]
    assert [a["id"] for a in card["actions"]] == ["confirm", "cancel"]
    # Not applied yet: hard dates move only after a confirm.
    assert str(vault.read_goals()[0]["deadline"]) == "2099-01-01"
    assert [c["id"] for c in _json(app, "GET", "/api/cards")[1]["cards"]] == [card["id"]]

    status, answer = _json(app, "POST", f"/api/cards/{card['id']}", {"action": "confirm"})
    assert status == 200 and answer["ok"] and "2099-02-01" in answer["message"]
    assert str(vault.read_goals()[0]["deadline"]) == "2099-02-01"
    # Answered once only.
    status, gone = _json(app, "POST", f"/api/cards/{card['id']}", {"action": "confirm"})
    assert (status, gone["error"]) == (410, "expired")
    assert _json(app, "GET", "/api/cards")[1]["cards"] == []
    entry = vault.read_journal_entry(date(2026, 10, 1))
    assert entry is not None and "surfed" in entry.transcript


def test_a_note_for_a_future_day_is_refused(app: App) -> None:
    status, body = _json(app, "POST", "/api/log", {"text": "x", "date": "2999-01-01"})
    assert (status, body["error"]) == (400, "bad_date")


def test_chat_replies_and_proposes_a_card(app: App, monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_respond(v: Vault, history: Any, text: str, now: datetime, **kw: Any) -> Any:
        return chat.ChatResult(
            text=f"echo {text}",
            proposals=[
                {
                    "edit": "itinerary",
                    "place": "Bali",
                    "status": "candidate",
                    "summary": "Add Bali to the wishlist",
                }
            ],
            research_jobs=[{"job": "rank", "place": "x"}],
            new_history=[{"role": "user", "content": text}, {"role": "assistant", "content": "e"}],
            cost=0.01,
        )

    monkeypatch.setattr(chat, "respond", fake_respond)
    status, body = _json(app, "POST", "/api/chat", {"text": "hello"})
    assert status == 200 and body["reply"] == "echo hello" and body["cost"] == 0.01
    [card] = body["cards"]
    assert card["kind"] == "chat_proposal" and card["title"] == "Add Bali to the wishlist"
    messages = _json(app, "GET", "/api/chat")[1]["messages"]
    assert [(m["role"], m["text"]) for m in messages] == [
        ("user", "hello"),
        ("assistant", "echo hello"),
    ]
    status, answer = _json(app, "POST", f"/api/cards/{card['id']}", {"action": "cancel"})
    assert status == 200 and answer["message"].startswith("Left as is")
    _json(app, "POST", "/api/chat/reset", {})
    assert _json(app, "GET", "/api/chat")[1]["messages"] == []


def test_revert_only_takes_a_recent_change(app: App) -> None:
    status, body = _json(app, "POST", "/api/changes/deadbeef/revert", {})
    assert (status, body["error"]) == (404, "unknown_change")


# -- the app shell ---------------------------------------------------------------------------


def test_signed_in_root_serves_the_app_shell(app: App, monkeypatch: pytest.MonkeyPatch) -> None:
    files = {"index.html": b"<!doctype html>shell", "app.js": b"js", "app.css": b"css"}
    monkeypatch.setattr(webapi, "static_file", files.get)
    response, body = _call(app, "GET", "/")
    assert body == b"<!doctype html>shell"
    assert response.getheader("Cache-Control") == "no-store"
    response, body = _call(app, "GET", "/app.js")
    assert body == b"js" and response.getheader("Cache-Control") == "no-cache"
    assert response.getheader("Content-Type", "").startswith("text/javascript")
    # Signed out: the guest view, whatever the shell.
    _, guest = _call(app, "GET", "/", signed_in=False)
    assert b'name="password"' in guest


def test_without_a_shell_the_private_view_is_served(
    app: App, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(webapi, "static_file", lambda name: None)
    _, page = _call(app, "GET", "/")
    assert b'action="/logout"' in page
    assert _call(app, "GET", "/app.css")[0].status == 404


def test_static_file_only_serves_the_shell_files() -> None:
    assert webapi.static_file("../api.py") is None
    assert webapi.static_file("secret.txt") is None


# -- the card store -------------------------------------------------------------------------


def test_cards_expire_and_reject_unknown_actions() -> None:
    store = CardStore(expiry_s=10)
    card = store.add("want", "Q?", lambda a: Outcome(a), now=0)
    with pytest.raises(UnknownAction):
        store.answer(card.id, "launch", now=1)
    assert [c.id for c in store.open(now=5)] == [card.id]
    assert store.answer(card.id, "confirm", now=20) is None
    keyed = store.add("ripple", "A", lambda a: Outcome(a), key="k")
    assert store.add("ripple", "B", lambda a: Outcome(a), key="k") is keyed


def test_note_count_counts_timed_headings() -> None:
    from daylog import journal_service

    assert journal_service.note_count("### 09:00\n\nA\n\n### 18:30\n\nB") == 2
    assert journal_service.note_count("plain") == 0


def test_logical_day_is_what_me_reports(app: App) -> None:
    status, me = _json(app, "GET", "/api/me")
    assert status == 200 and me["signed_in"] is True
    assert me["today"] == daily.logical_day(datetime.now(tz())).isoformat()
    assert me["tz"] == str(tz())


def _logged(app: App, note: dict[str, Any], **kw: Any) -> Any:
    """POST a note (answered 202 with a job), then read the job's result."""
    status, accepted = _json(app, "POST", "/api/log", note, **kw)
    assert status == 202, accepted
    status, job = _json(app, "GET", f"/api/jobs/{accepted['job']}")
    assert status == 200 and job["status"] == "done", job
    return job["result"]


def test_a_note_sent_again_is_logged_once(
    app: App, vault: Vault, monkeypatch: pytest.MonkeyPatch
) -> None:
    # 2026-10-22: one voice note reached the server three times (the app was
    # closed mid-request, then Retry) and was logged three times.
    calls: list[str] = []

    def fake_extract(transcript: str, *args: Any, **kwargs: Any) -> dict[str, Any]:
        calls.append(transcript)
        return {"summary": "Foiled.", "activities": [{"type": "surf", "hours": 1}]}

    monkeypatch.setattr(extract, "extract", fake_extract)
    note = {"text": "foiled with Oliver", "date": "2026-10-01"}
    keyed = {"Idempotency-Key": "rec-1"}
    first = _json(app, "POST", "/api/log", note, headers=keyed)[1]
    again = _json(app, "POST", "/api/log", note, headers=keyed)[1]
    assert again["job"] == first["job"] and again["repeated"] is True
    # No key: the same text for the same day is still the same note.
    plain = _json(app, "POST", "/api/log", note)[1]
    assert _json(app, "POST", "/api/log", note)[1]["job"] == plain["job"]
    assert len(calls) == 2  # once for the keyed note, once for the keyless one
    entry = vault.read_journal_entry(date(2026, 10, 1))
    assert entry is not None and journal_service.note_count(entry.transcript) == 2
    jobs = _json(app, "GET", "/api/jobs")[1]["jobs"]
    assert {j["id"] for j in jobs} == {first["job"], plain["job"]}
    assert _json(app, "GET", "/api/jobs/0123456789abcdef")[0] == 404


def test_only_a_failed_job_runs_again() -> None:
    from daylog.webapp.jobs import Jobs

    pending: list[Any] = []
    jobs = Jobs(run=pending.append)  # run nothing until told
    job, new = jobs.submit("k", "voice", lambda: (200, {"ok": True}))
    assert new and job.status == "processing"
    assert jobs.submit("k", "voice", lambda: (200, {}))[1] is False  # still running: same job
    pending.pop()()
    assert jobs.get(job.id) is not None and job.status == "done"
    assert jobs.submit("k", "voice", lambda: (200, {}))[0] is job  # done: same answer

    bad, _ = jobs.submit("b", "voice", lambda: (500, {"message": "boom"}))
    pending.pop()()
    assert bad.status == "failed" and bad.view()["result"]["message"] == "boom"
    retry, new = jobs.submit("b", "voice", lambda: (200, {}))
    assert new and retry is not bad  # a failure may run again
