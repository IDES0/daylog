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
