from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from daylog import transcribe, vocab
from tests.place_fixtures import tree


def test_terms_put_the_users_words_first_then_nearby_places_then_sports() -> None:
    profile = {"vocabulary": ["NASCO", "Priority SUP", "nasco"]}
    disciplines = {
        "water": {"name": "Water"},
        "surf": {"name": "Surfing", "parents": ["water"], "status": "active"},
    }
    words = vocab.terms(profile, tree(), {"place": "Kuta, Lombok", "place_id": "kuta"}, disciplines)
    assert words[:2] == ["NASCO", "Priority SUP"]  # "nasco" is the same word
    assert "Ekas Bay" in words and "Lombok" in words
    assert words[-1] == "Surfing" and "Water" not in words  # groupings aren't said
    # The region's own spots are in, said as spoken ("Kuta (wind foiling)" is
    # "Kuta", already there); another island is noise.
    assert "Kuta" in words and not any("(" in w for w in words) and "Bali" not in words


def test_terms_stay_short() -> None:
    profile = {"vocabulary": [f"word{i:03d}" for i in range(400)]}
    words = vocab.terms(profile, [], None, {})
    assert sum(len(w) + 2 for w in words) <= vocab.MAX_CHARS
    assert words[0] == "word000"


def test_primer_reads_like_text_before_the_note() -> None:
    assert vocab.primer(["NASCO", "Uluwatu"]) == "Words in this note may include: NASCO, Uluwatu."
    assert vocab.primer([]) is None


def test_primer_for_never_fails_a_note() -> None:
    class Broken:
        def read_profile(self) -> Any:
            raise OSError("vault unreadable")

    assert vocab.primer_for(Broken()) is None  # type: ignore[arg-type]


def test_transcribe_passes_the_primer_to_whisper(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: dict[str, Any] = {}

    class Info:
        language = "en"
        duration = 1.0

    class Seg:
        text = " surfed at Uluwatu "

    class Model:
        def transcribe(self, path: str, **kw: Any) -> tuple[list[Seg], Info]:
            seen.update(kw)
            return [Seg()], Info()

    monkeypatch.setattr(transcribe, "_acquire", lambda size: Model())
    monkeypatch.setattr(transcribe, "_release", lambda: None)
    text = transcribe.transcribe(Path("x.ogg"), primer="Words in this note may include: NASCO.")
    assert text == "surfed at Uluwatu"
    assert seen["initial_prompt"] == "Words in this note may include: NASCO."
