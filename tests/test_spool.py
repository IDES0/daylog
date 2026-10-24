"""Web notes survive a restart: spooled before the 202, resumed on start."""

from __future__ import annotations

import json
import subprocess
from collections.abc import Callable
from datetime import date
from pathlib import Path
from typing import Any

import pytest

from daylog import extract, journal_service, transcribe
from daylog.vault import Vault
from daylog.webapp import api as webapi
from daylog.webapp import jobs as jobs_mod

Queue = list[Callable[[], None]]


def _api(vault: Vault, queue: Queue | None = None) -> webapi.Api:
    """A fresh Api, as after a restart. With a queue, notes wait in it until run."""
    run = queue.append if queue is not None else (lambda fn: fn())
    return webapi.Api(vault=lambda: vault, spawn=lambda fn: None, run_notes=run)


def _post(api: webapi.Api, path: str, body: bytes, **headers: str) -> tuple[int, Any]:
    req = webapi.Request(
        method="POST",
        path=path.split("?")[0],
        query={"date": [path.split("=")[1]]} if "?" in path else {},
        headers={"x-daylog": "1", **{k.lower(): v for k, v in headers.items()}},
        body=body,
        signed_in=True,
    )
    return api.handle(req)


def _get(api: webapi.Api, path: str) -> tuple[int, Any]:
    return api.handle(webapi.Request(method="GET", path=path, signed_in=True))


def _note(text: str, day: str = "2026-10-01") -> bytes:
    return json.dumps({"text": text, "date": day}).encode()


def _git(vault: Vault, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=vault.path, capture_output=True, text=True, check=True
    ).stdout


@pytest.fixture
def extracted(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    calls: list[str] = []

    def fake_extract(transcript: str, *args: Any, **kwargs: Any) -> dict[str, Any]:
        calls.append(transcript)
        return {"summary": "Foiled.", "activities": [{"type": "surf", "hours": 1}]}

    monkeypatch.setattr(extract, "extract", fake_extract)
    return calls


def test_a_note_cut_off_by_a_restart_is_logged_once(vault: Vault, extracted: list[str]) -> None:
    queue: Queue = []
    before = _api(vault, queue)
    status, accepted = _post(
        before, "/api/log", _note("foiled at dawn"), **{"Idempotency-Key": "n1"}
    )
    assert status == 202
    assert queue  # accepted, not yet logged — then the server goes down
    record = json.loads((vault.spool_path / f"{accepted['job']}.json").read_text())
    assert record["status"] == "processing" and record["note"]["text"] == "foiled at dawn"

    after = _api(vault)
    after.resume()
    status, job = _get(after, f"/api/jobs/{accepted['job']}")
    assert status == 200 and job["status"] == "done", job
    assert extracted == ["foiled at dawn"]
    # Sent again (Retry) after the restart: the same job, not a second log.
    again = _post(after, "/api/log", _note("foiled at dawn"), **{"Idempotency-Key": "n1"})[1]
    assert again["job"] == accepted["job"] and again["repeated"] is True
    # Another restart doesn't run it again.
    _api(vault).resume()
    assert extracted == ["foiled at dawn"]
    entry = vault.read_journal_entry(date(2026, 10, 1))
    assert entry is not None and journal_service.note_count(entry.transcript) == 1
    assert "note" not in json.loads((vault.spool_path / f"{accepted['job']}.json").read_text())


def test_a_finished_job_is_still_known_after_a_restart(vault: Vault, extracted: list[str]) -> None:
    accepted = _post(_api(vault), "/api/log", _note("kited"))[1]
    after = _api(vault)
    after.resume()
    status, job = _get(after, f"/api/jobs/{accepted['job']}")
    assert status == 200 and job["status"] == "done" and job["result"]["summary"] == "Foiled."
    assert [j["id"] for j in _get(after, "/api/jobs")[1]["jobs"]] == [accepted["job"]]
    assert extracted == ["kited"]


def test_voice_audio_is_spooled_then_dropped(
    vault: Vault, extracted: list[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    heard: list[bytes] = []

    def fake_transcribe(path: Path, primer: str = "") -> str:
        heard.append(path.read_bytes())
        return "wing foiled two hours"

    monkeypatch.setattr(transcribe, "transcribe", fake_transcribe)
    queue: Queue = []
    status, accepted = _post(
        _api(vault, queue), "/api/voice?date=2026-10-01", b"OPUS", **{"Content-Type": "audio/ogg"}
    )
    assert status == 202
    audio = vault.spool_path / f"{accepted['job']}.audio"
    assert audio.read_bytes() == b"OPUS"

    after = _api(vault)
    after.resume()
    job = _get(after, f"/api/jobs/{accepted['job']}")[1]
    assert job["status"] == "done" and job["result"]["transcript"] == "wing foiled two hours"
    assert heard == [b"OPUS"] and not audio.exists()
    # The spool never reaches the vault's git.
    assert _git(vault, "status", "--porcelain") == ""
    assert not [f for f in _git(vault, "ls-files").splitlines() if f.startswith(".spool")]
    assert "/.spool/" in (vault.path / ".git" / "info" / "exclude").read_text()


def test_a_failed_note_keeps_its_transcript_and_drops_its_audio(
    vault: Vault, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(transcribe, "transcribe", lambda path, primer="": "paddled out")

    def broken(*args: Any, **kwargs: Any) -> Any:
        raise RuntimeError("model down")

    monkeypatch.setattr(journal_service, "log", broken)
    accepted = _post(_api(vault), "/api/voice", b"OPUS", **{"Content-Type": "audio/ogg"})[1]
    after = _api(vault)
    after.resume()
    job = _get(after, f"/api/jobs/{accepted['job']}")[1]
    assert job["status"] == "failed" and job["result"]["text"] == "paddled out"
    assert not (vault.spool_path / f"{accepted['job']}.audio").exists()


def test_a_note_that_keeps_crashing_is_given_up(vault: Vault, extracted: list[str]) -> None:
    queue: Queue = []
    accepted = _post(_api(vault, queue), "/api/log", _note("cursed"))[1]
    for _ in range(jobs_mod.MAX_ATTEMPTS - 1):  # each restart starts it, then dies again
        _api(vault, []).resume()
    after = _api(vault)
    after.resume()
    job = _get(after, f"/api/jobs/{accepted['job']}")[1]
    assert job["status"] == "failed" and job["result"]["text"] == "cursed"
    assert extracted == []


def test_old_records_are_pruned_on_start(vault: Vault, extracted: list[str]) -> None:
    accepted = _post(_api(vault), "/api/log", _note("old news"))[1]
    path = vault.spool_path / f"{accepted['job']}.json"
    record = json.loads(path.read_text())
    record["started"] -= jobs_mod.TTL_S + 60
    path.write_text(json.dumps(record))
    after = _api(vault)
    after.resume()
    assert _get(after, f"/api/jobs/{accepted['job']}")[0] == 404
    assert not path.exists()
