from __future__ import annotations

import pytest

from daylog.extract import ExtractError, _validate_shape


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
