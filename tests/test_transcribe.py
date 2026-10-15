from __future__ import annotations

import sys
import types
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from daylog import transcribe


class _FakeModel:
    loads = 0

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        _FakeModel.loads += 1

    def transcribe(self, path: str) -> tuple[list[Any], Any]:
        segment = types.SimpleNamespace(text=" hello there ")
        info = types.SimpleNamespace(language="en", duration=1.0)
        return [segment], info


@pytest.fixture
def fake_whisper(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    module = types.ModuleType("faster_whisper")
    module.WhisperModel = _FakeModel  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "faster_whisper", module)
    # Long enough that the timer never fires on its own during a test.
    monkeypatch.setattr(transcribe, "IDLE_UNLOAD_SECONDS", 3600.0)
    _FakeModel.loads = 0
    transcribe.unload()
    yield
    if transcribe._unload_timer is not None:
        transcribe._unload_timer.cancel()
    transcribe.unload()


def test_loads_on_first_use_and_reuses(fake_whisper: None) -> None:
    assert not transcribe.is_loaded()
    assert transcribe.transcribe(Path("a.ogg")) == "hello there"
    assert transcribe.transcribe(Path("b.ogg")) == "hello there"
    assert transcribe.is_loaded()
    assert _FakeModel.loads == 1
    assert transcribe._unload_timer is not None


def test_unload_then_reload(fake_whisper: None) -> None:
    transcribe.transcribe(Path("a.ogg"))
    transcribe.unload()
    assert not transcribe.is_loaded()
    transcribe.transcribe(Path("b.ogg"))
    assert transcribe.is_loaded()
    assert _FakeModel.loads == 2


def test_unload_skipped_while_in_use(fake_whisper: None) -> None:
    transcribe._acquire("small")
    transcribe.unload()
    assert transcribe.is_loaded()
    transcribe._release()
    transcribe.unload()
    assert not transcribe.is_loaded()


def test_idle_timer_unloads(fake_whisper: None, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(transcribe, "IDLE_UNLOAD_SECONDS", 0.01)
    transcribe.transcribe(Path("a.ogg"))
    timer = transcribe._unload_timer
    assert timer is not None
    timer.join(timeout=2)
    assert not transcribe.is_loaded()
