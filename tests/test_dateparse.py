from __future__ import annotations

from datetime import date

import pytest

from daylog.dateparse import parse_date_phrase

TODAY = date(2026, 8, 21)


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("today", TODAY),
        ("Today", TODAY),
        ("  today  ", TODAY),
        ("yesterday", date(2026, 8, 20)),
        ("Yesterday", date(2026, 8, 20)),
        ("2 days ago", date(2026, 8, 19)),
        ("1 day ago", date(2026, 8, 20)),
        ("2026-08-19", date(2026, 8, 19)),
        ("august 20", date(2026, 8, 20)),
        ("Aug 20", date(2026, 8, 20)),
        ("august 20, 2025", date(2025, 8, 20)),
        ("aug 20 2025", date(2025, 8, 20)),
    ],
)
def test_parse_date_phrase_recognized(text: str, expected: date) -> None:
    assert parse_date_phrase(text, today=TODAY) == expected


@pytest.mark.parametrize(
    "text",
    [
        "",
        "   ",
        "went surfing yesterday at Echo Beach for two hours",
        "not a date at all",
        "sometime next week maybe",
    ],
)
def test_parse_date_phrase_unrecognized_returns_none(text: str) -> None:
    assert parse_date_phrase(text, today=TODAY) is None
