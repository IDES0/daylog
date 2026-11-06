from __future__ import annotations

from typing import Any, cast

import pytest

from daylog.extract import RECORD_JOURNAL_ENTRY_TOOL, ExtractError, _validate_shape


def test_validate_shape_accepts_well_formed_facts() -> None:
    _validate_shape({"activities": [{"type": "surf"}], "summary": "Surfed."})  # must not raise


def test_validate_shape_rejects_string_where_array_expected() -> None:
    # Reproduces the real production failure: the model returned a JSON
    # string as the value of "activities" instead of a list — this must be
    # caught here rather than silently written to the vault (see journal
    # 2026-09-16, which had to be manually repaired after this shipped).
    with pytest.raises(ExtractError, match="activities"):
        _validate_shape({"activities": '{"activities": []}', "summary": "x"})


def test_validate_shape_rejects_missing_summary() -> None:
    with pytest.raises(ExtractError, match="summary"):
        _validate_shape({"activities": []})


def test_validate_shape_rejects_empty_summary() -> None:
    with pytest.raises(ExtractError, match="summary"):
        _validate_shape({"activities": [], "summary": "   "})


def test_validate_shape_allows_absent_optional_array_fields() -> None:
    # goal_progress, corrections, etc. are legitimately omitted most of the
    # time — only a present-but-wrong-typed value should raise.
    _validate_shape({"activities": [], "summary": "x"})  # must not raise


def test_schema_has_nutrition_estimates_and_mood_focus_scores() -> None:
    schema = cast(dict[str, Any], RECORD_JOURNAL_ENTRY_TOOL["input_schema"])
    meal = schema["properties"]["meals"]["items"]["properties"]
    assert {"kcal", "protein_g", "carbs_g", "fat_g"} <= set(meal)
    felt = schema["properties"]["felt"]["items"]["properties"]
    assert {"energy", "mood", "focus"} <= set(felt)


class _Block:
    type = "tool_use"

    def __init__(self, data: dict[str, Any]) -> None:
        self.input = data


class _Usage:
    input_tokens = 1
    output_tokens = 1
    cache_read_input_tokens = 0
    cache_creation_input_tokens = 0


class _Response:
    stop_reason = "tool_use"
    usage = _Usage()

    def __init__(self, data: dict[str, Any]) -> None:
        self.content = [_Block(data)]


class _Client:
    def __init__(self, *payloads: dict[str, Any]) -> None:
        self.payloads = list(payloads)
        self.calls = 0
        self.messages = self

    def create(self, **kwargs: Any) -> _Response:
        self.calls += 1
        self.sent = kwargs
        return _Response(self.payloads.pop(0))


def test_activities_can_carry_a_rough_start_and_the_note_time_is_given(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from datetime import date, time

    from daylog import extract, llm

    schema = cast(dict[str, Any], RECORD_JOURNAL_ENTRY_TOOL["input_schema"])
    assert "at" in schema["properties"]["activities"]["items"]["properties"]

    monkeypatch.setattr(llm, "record", lambda *a, **k: None)
    payload = {"activities": [{"type": "surf", "at": "06:00"}], "summary": "Surfed."}
    client = _Client(payload, payload)
    day = date(2026, 10, 3)
    extract.extract("surfed", [], [], day, recorded_at=time(9, 5), client=client)  # type: ignore[arg-type]
    assert "Note recorded at: 09:05" in client.sent["messages"][0]["content"]
    extract.extract("surfed", [], [], day, client=client)  # type: ignore[arg-type]
    assert "Note recorded at" not in client.sent["messages"][0]["content"]


def test_extract_retries_once_after_a_malformed_tool_call(monkeypatch: pytest.MonkeyPatch) -> None:
    from datetime import date

    from daylog import extract, llm

    monkeypatch.setattr(llm, "record", lambda *a, **k: None)
    good = {"activities": [{"type": "surf"}], "summary": "Surfed."}
    client = _Client({"activities": "surfed for 2 hours", "summary": "Surfed."}, good)
    facts = extract.extract("surfed", [], [], date(2026, 10, 3), client=client)  # type: ignore[arg-type]
    assert facts == good and client.calls == 2

    bad = {"activities": "nope", "summary": "x"}
    client = _Client(bad, bad)
    with pytest.raises(ExtractError):
        extract.extract("surfed", [], [], date(2026, 10, 3), client=client)  # type: ignore[arg-type]
    assert client.calls == extract.EXTRACT_ATTEMPTS


def test_repair_parses_a_stringified_list_or_whole_payload() -> None:
    from daylog.extract import _repair

    assert _repair({"activities": '[{"type": "surf"}]', "summary": "S"}) == {
        "activities": [{"type": "surf"}],
        "summary": "S",
    }
    whole = '{"activities": [{"type": "surf"}], "summary": "Surfed.", "meals": []}'
    assert _repair({"activities": whole}) == {
        "activities": [{"type": "surf"}],
        "summary": "Surfed.",
        "meals": [],
    }
    # not JSON, or JSON of the wrong shape: left for _validate_shape to reject
    assert _repair({"activities": "surfed 2h"})["activities"] == "surfed 2h"
    assert _repair({"activities": '{"weird": 1}'})["activities"] == '{"weird": 1}'


def test_extract_sends_the_discipline_outline_and_goal_disciplines(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from datetime import date

    from daylog import extract, llm

    monkeypatch.setattr(llm, "record", lambda *a, **k: None)
    seen: dict[str, Any] = {}

    class _Capture(_Client):
        def create(self, **kwargs: Any) -> _Response:
            seen.update(kwargs)
            return super().create(**kwargs)

    goals = [{"id": "surf", "title": "Surfing", "type": "soft", "discipline": "surf"}]
    client = _Capture({"activities": [{"type": "foil"}], "summary": "Foiled."})
    extract.extract(
        "foiled",
        goals,
        [],
        date(2026, 10, 15),
        disciplines="- foil: Foiling",
        client=client,  # type: ignore[arg-type]
    )
    content = seen["messages"][0]["content"]
    assert (
        "Disciplines (id: name; indented under what it is a kind of):\n- foil: Foiling" in content
    )
    assert "discipline: surf" in content


def test_extract_reads_the_note_against_the_recent_days(monkeypatch: pytest.MonkeyPatch) -> None:
    from datetime import date

    from daylog import extract, llm

    monkeypatch.setattr(llm, "record", lambda *a, **k: None)
    schema = cast(dict[str, Any], RECORD_JOURNAL_ENTRY_TOOL["input_schema"])
    assert "heard_as" in schema["properties"]["activities"]["items"]["properties"]
    recent = {
        date(2026, 11, 3): {
            "activities": [
                {"type": "surf", "hours": 2, "detail": "two sessions", "place": "waco-surf"},
                {"type": "screen_time"},
            ],
            "decisions": ["Booked the Waco Surf winter pass"],
        },
        date(2026, 11, 4): {"meals": [{"items": ["tacos"]}]},
    }
    places = [{"id": "waco-surf", "name": "Waco Surf", "kind": "surf_spot"}]
    client = _Client({"activities": [{"type": "surf"}], "summary": "Surfed."})
    day = date(2026, 11, 5)
    extract.extract("Wakefoil", [], [], day, places=places, recent=recent, client=client)  # type: ignore[arg-type]
    content = client.sent["messages"][0]["content"]
    assert "Recent days (context only" in content
    assert "- 2026-11-03 Tue: surf, 2h — two sessions @ Waco Surf; screen_time" in content
    assert "decided: Booked the Waco Surf winter pass" in content
    # A day with nothing done is left out.
    assert "2026-11-04" not in content
    extract.extract("x", [], [], day, client=_Client({"summary": "x"}))  # type: ignore[arg-type]


def test_day_scores_have_a_fixed_shape_and_bad_ones_are_dropped(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from datetime import date

    from daylog import extract, llm

    schema = cast(dict[str, Any], RECORD_JOURNAL_ENTRY_TOOL["input_schema"])
    rating = schema["properties"]["rating"]
    assessment = schema["properties"]["assessment"]
    assert rating["properties"]["score"]["minimum"] == 1
    assert rating["properties"]["score"]["maximum"] == 10
    assert rating["required"] == ["score"] and "note" in rating["properties"]
    assert assessment["required"] == ["score", "why"]

    monkeypatch.setattr(llm, "record", lambda *a, **k: None)
    payload = {
        "activities": [],
        "rating": {"score": 7.0, "note": "  good waves  "},
        "assessment": {"score": 11, "why": "off the scale"},
        "summary": "Surfed.",
    }
    facts = extract.extract("a 7", [], [], date(2026, 11, 2), client=_Client(payload))  # type: ignore[arg-type]
    assert facts["rating"] == {"score": 7, "note": "good waves"}
    assert "assessment" not in facts  # out of range: dropped, the note still logs

    assert extract.day_score({"score": True}, "note") is None
    assert extract.day_score("7", "note") is None
    assert extract.day_score({"score": 3}, "why") == {"score": 3}


def test_a_single_note_never_writes_an_assessment(
    vault: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    from datetime import date, datetime

    from daylog import extract, journal_service

    def fake_extract(*args: Any, **kwargs: Any) -> dict[str, Any]:
        return {
            "activities": [],
            "rating": {"score": 8},
            "assessment": {"score": 6, "why": "guessing from one note"},
            "summary": "A good day.",
        }

    monkeypatch.setattr(extract, "extract", fake_extract)
    day = date(2026, 11, 2)
    journal_service.log(vault, "solid 8 out of 10 day", day, datetime(2026, 11, 2, 21, 0))
    entry = vault.read_journal_entry(day)
    assert entry.frontmatter["rating"] == {"score": 8}
    assert "assessment" not in entry.frontmatter
