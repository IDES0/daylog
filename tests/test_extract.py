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

    def create(self, **_: Any) -> _Response:
        self.calls += 1
        return _Response(self.payloads.pop(0))


def test_extract_retries_once_after_a_malformed_tool_call(monkeypatch: pytest.MonkeyPatch) -> None:
    from datetime import date

    from daylog import extract, llm

    monkeypatch.setattr(llm, "record", lambda *a, **k: None)
    good = {"activities": [{"type": "surf"}], "summary": "Surfed."}
    client = _Client({"activities": '[{"type": "surf"}]', "summary": "Surfed."}, good)
    facts = extract.extract("surfed", [], [], date(2026, 10, 3), client=client)  # type: ignore[arg-type]
    assert facts == good and client.calls == 2

    bad = {"activities": "nope", "summary": "x"}
    client = _Client(bad, bad)
    with pytest.raises(ExtractError):
        extract.extract("surfed", [], [], date(2026, 10, 3), client=client)  # type: ignore[arg-type]
    assert client.calls == extract.EXTRACT_ATTEMPTS
