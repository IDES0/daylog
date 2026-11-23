"""The watch inlet: device tokens, /watch/log and /watch/ask (invented data, no API calls)."""

from __future__ import annotations

import http.client
import json
import threading
from collections.abc import Callable, Iterator
from datetime import date, datetime
from http.server import ThreadingHTTPServer
from typing import Any

import pytest

from daylog import calendar_server, chat, extract, web
from daylog.runtime import tz
from daylog.vault import Vault
from daylog.webapp import api as webapi
from daylog.webapp import watch

TODAY = date(2026, 11, 23)
NOW = datetime(2026, 11, 23, 12, 0, tzinfo=tz())
PASSWORD = "right horse"


class Notes:
    """Stands in for push.send: records what would be sent."""

    def __init__(self) -> None:
        self.sent: list[tuple[str, str, str]] = []

    def __call__(self, v: Vault, title: str, body: str, url: str) -> int:
        self.sent.append((title, body, url))
        return 1


def _api(vault: Vault) -> webapi.Api:
    return webapi.Api(
        vault=lambda: vault, now=lambda: NOW, spawn=lambda fn: None, run_notes=lambda fn: fn()
    )


def _req(path: str, token: str, body: Any) -> watch.Request:
    raw = body if isinstance(body, bytes) else json.dumps(body).encode()
    return watch.Request(
        path, {"authorization": f"Bearer {token}", "content-type": "application/json"}, raw
    )


def _fake_extract(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake(transcript: str, *args: Any, **kwargs: Any) -> dict[str, Any]:
        return {
            "summary": "Surfed two hours at dawn.",
            "activities": [{"type": "surf", "hours": 2}],
        }

    monkeypatch.setattr(extract, "extract", fake)


def _fake_chat(monkeypatch: pytest.MonkeyPatch, gate: threading.Event | None = None) -> None:
    def fake(v: Vault, history: Any, text: str, now: datetime, **kw: Any) -> Any:
        if gate is not None:
            gate.wait(5)
        return chat.ChatResult(
            text="**Yes.** You slept *7 h* — see [the log](https://x.test/a).\n- item one",
            proposals=[],
            research_jobs=[],
            new_history=[{"role": "user", "content": text}, {"role": "assistant", "content": "y"}],
            cost=0.0,
        )

    monkeypatch.setattr(chat, "respond", fake)


def test_tokens_are_shown_once_kept_hashed_and_revocable(vault: Vault) -> None:
    device, token = watch.add(vault, "Alex's <Watch>", TODAY)
    assert token.startswith("dl_") and len(token) > 30
    stored = vault.read_yaml(watch.FILE, {})["devices"][0]
    assert stored["name"] == "Alex's Watch" and token not in json.dumps(dict(stored))
    assert watch.public(stored) == {
        "id": device["id"],
        "name": "Alex's Watch",
        "created": "2026-11-23",
        "revoked": None,
    }
    assert watch.find(vault, token)["id"] == device["id"]  # type: ignore[index]
    assert watch.find(vault, token + "x") is None and watch.find(vault, "nope") is None
    assert watch.revoke(vault, device["id"], TODAY)
    assert watch.find(vault, token) is None
    assert not watch.revoke(vault, device["id"], TODAY)


def test_refusals_before_anything_runs(vault: Vault) -> None:
    w = watch.Watch(_api(vault), notify=Notes())
    _, token = watch.add(vault, "Watch", TODAY)
    assert w.handle(_req("/watch/log", "dl_wrong", {"text": "x"}))[0] == 401
    assert w.handle(_req("/watch/other", token, {"text": "x"}))[0] == 404
    assert w.handle(_req("/watch/log", token, {"text": "  "}))[0] == 400
    assert w.handle(_req("/watch/log", token, b"x" * (watch.MAX_BODY_BYTES + 1)))[0] == 413


def test_too_many_bad_tokens_lock_everyone_out_for_a_while(vault: Vault) -> None:
    t = [0.0]
    w = watch.Watch(_api(vault), notify=Notes(), clock=lambda: t[0])
    _, token = watch.add(vault, "Watch", TODAY)
    for _ in range(watch.MAX_MISSES):
        assert w.handle(_req("/watch/log", "dl_bad", {"text": "x"}))[0] == 401
    assert w.handle(_req("/watch/log", token, {"text": "x"}))[0] == 429
    t[0] = watch.MISS_WINDOW_S + 1
    assert w.handle(_req("/watch/log", token, {"text": ""}))[0] == 400  # through the gate again


def test_a_device_is_rate_limited(vault: Vault) -> None:
    w = watch.Watch(_api(vault), notify=Notes(), clock=lambda: 0.0)
    _, token = watch.add(vault, "Watch", TODAY)
    codes = [
        w.handle(_req("/watch/log", token, {"text": ""}))[0] for _ in range(watch.RATE_LIMIT + 1)
    ]
    assert codes[:-1] == [400] * watch.RATE_LIMIT and codes[-1] == 429


def test_log_runs_the_journal_pipeline_and_notifies_once(
    vault: Vault, monkeypatch: pytest.MonkeyPatch
) -> None:
    _fake_extract(monkeypatch)
    notes = Notes()
    w = watch.Watch(_api(vault), notify=notes)
    _, token = watch.add(vault, "Watch", TODAY)
    code, text = w.handle(_req("/watch/log", token, {"text": "surfed two hours at dawn"}))
    assert code == 202 and "Logging now" in text
    entry = vault.read_journal_entry(TODAY)
    assert entry is not None and "surfed two hours at dawn" in entry.transcript
    assert notes.sent == [("📝 Logged", "Surfed two hours at dawn.", "/#/home")]
    # Plain text works too, the same note isn't logged twice.
    raw = watch.Request(
        "/watch/log", {"authorization": f"Bearer {token}"}, b"surfed two hours at dawn"
    )
    assert w.handle(raw)[0] == 202
    assert len(notes.sent) == 1


def test_ask_answers_in_time_as_plain_text_without_a_notification(
    vault: Vault, monkeypatch: pytest.MonkeyPatch
) -> None:
    _fake_chat(monkeypatch)
    notes = Notes()
    w = watch.Watch(_api(vault), spawn=lambda fn: fn(), notify=notes)
    _, token = watch.add(vault, "Watch", TODAY)
    code, text = w.handle(_req("/watch/ask", token, {"text": "did I sleep enough?"}))
    assert (code, text) == (200, "Yes. You slept 7 h — see the log.\nitem one")
    assert notes.sent == []
    # Same conversation as the Chat tab.
    assert [m["text"] for m in w.api.chat.messages()][0] == "did I sleep enough?"


def test_a_slow_answer_comes_as_one_notification(
    vault: Vault, monkeypatch: pytest.MonkeyPatch
) -> None:
    gate = threading.Event()
    _fake_chat(monkeypatch, gate)
    monkeypatch.setattr(watch, "WAIT_SECONDS", 0.05)
    notes = Notes()
    threads: list[threading.Thread] = []

    def spawn(fn: Callable[[], None]) -> None:
        threads.append(threading.Thread(target=fn))
        threads[-1].start()

    w = watch.Watch(_api(vault), spawn=spawn, notify=notes)
    _, token = watch.add(vault, "Watch", TODAY)
    code, text = w.handle(_req("/watch/ask", token, {"text": "plan my week"}))
    assert (code, text) == (202, "Thinking. The answer will come as a notification.")
    gate.set()
    threads[0].join(5)
    assert notes.sent == [("daylog", "Yes. You slept 7 h — see the log.\nitem one", "/#/chat")]


@pytest.fixture
def server(vault: Vault, monkeypatch: pytest.MonkeyPatch) -> Iterator[int]:
    monkeypatch.setenv("VAULT_PATH", str(vault.path))
    handler = calendar_server._make_handler(None, None, True, PASSWORD, app=_api(vault))
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    yield httpd.server_address[1]
    httpd.shutdown()


def _http(
    port: int, method: str, path: str, body: Any = None, headers: dict[str, str] | None = None
) -> tuple[int, bytes]:
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
    data = json.dumps(body).encode() if body is not None else None
    conn.request(
        method, path, body=data, headers={"Content-Type": "application/json", **(headers or {})}
    )
    r = conn.getresponse()
    return r.status, r.read()


def test_devices_are_managed_in_the_app_and_used_by_the_watch(
    server: int, monkeypatch: pytest.MonkeyPatch
) -> None:
    _fake_chat(monkeypatch)
    signed = {"Cookie": f"{web.SESSION_COOKIE}={web.make_session(PASSWORD)}", "X-Daylog": "1"}
    assert _http(server, "POST", "/api/devices", {"name": "Watch"})[0] == 401  # session first
    code, raw = _http(server, "POST", "/api/devices", {"name": "Ultra"}, signed)
    made = json.loads(raw)
    assert code == 200 and made["device"]["name"] == "Ultra"
    assert made["urls"]["ask"].endswith("/watch/ask")
    listed = json.loads(_http(server, "GET", "/api/devices", None, signed)[1])
    assert "hash" not in listed["devices"][0] and "token" not in listed
    # The watch path takes the token, never the session.
    assert _http(server, "POST", "/watch/ask", {"text": "hi"}, signed)[0] == 401
    code, raw = _http(
        server, "POST", "/watch/ask", {"text": "hi"}, {"Authorization": f"Bearer {made['token']}"}
    )
    assert code == 200 and raw.decode().startswith("Yes. You slept")
    did = made["device"]["id"]
    assert _http(server, "POST", f"/api/devices/{did}/revoke", {}, signed)[0] == 200
    assert (
        _http(
            server,
            "POST",
            "/watch/ask",
            {"text": "hi"},
            {"Authorization": f"Bearer {made['token']}"},
        )[0]
        == 401
    )
