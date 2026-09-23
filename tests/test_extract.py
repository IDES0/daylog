from __future__ import annotations

import pytest

from daylog.extract import ExtractError, _format_known_spots, _validate_shape

_PLACES = [
    {
        "name": "Lombok, Indonesia",
        "surf_spots": [
            {"name": "Gerupuk (Inside Right)", "break_type": "right"},
            {"name": "Ekas Bay (Inside)", "break_type": "right/left peak"},
        ],
        "wind_spots": [{"name": "Kuta (wind foiling)"}],
    },
    {"name": "Siargao, Philippines", "surf_spots": [{"name": "Cloud 9", "break_type": "right"}]},
]


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


def test_format_known_spots_lists_name_and_break_type() -> None:
    text = _format_known_spots(_PLACES)
    assert "Gerupuk (Inside Right) (Lombok, Indonesia, right)" in text
    assert "Cloud 9 (Siargao, Philippines, right)" in text


def test_format_known_spots_empty_list() -> None:
    assert _format_known_spots([]) == "(none curated yet)"
