from __future__ import annotations

from datetime import date

from daylog.itinerary import (
    apply_confirmed_change,
    apply_itinerary_changes,
    current_date,
    find_entry,
)

ON = date(2026, 8, 21)


def _itinerary() -> list[dict[str, object]]:
    return [
        {
            "id": "canggu-bali-id",
            "place": "Canggu, Bali, ID",
            "type": "soft",
            "status": "current",
            "target_window": ["2026-08-01", None],
        },
        {
            "id": "indonesia-visa-exit",
            "place": "Indonesia (exit)",
            "type": "hard",
            "status": "active",
        },
    ]


def test_current_date_prefers_deadline_over_window() -> None:
    hard = {"deadline": "2026-10-15", "target_window": ["2026-01-01", "2026-01-02"]}
    assert current_date(hard) == "2026-10-15"


def test_current_date_falls_back_to_window_end() -> None:
    soft = {"target_window": ["2026-09-01", "2026-10-01"]}
    assert current_date(soft) == "2026-10-01"


def test_current_date_none_when_unset() -> None:
    assert current_date({}) is None


def test_new_soft_entry_applies_immediately() -> None:
    itin = _itinerary()
    applied, pending = apply_itinerary_changes(
        itin, [{"place": "Vietnam", "status": "candidate"}], on=ON
    )

    assert pending == []
    assert len(applied) == 1
    entry = find_entry(itin, applied[0].id)
    assert entry is not None
    assert entry["place"] == "Vietnam"
    assert entry["type"] == "soft"
    assert entry["status"] == "candidate"


def test_new_entry_id_is_slugified_and_deduped() -> None:
    itin = _itinerary()
    apply_itinerary_changes(itin, [{"place": "Ho Chi Minh City!"}], on=ON)
    apply_itinerary_changes(itin, [{"place": "Ho Chi Minh City!"}], on=ON)

    ids = [e["id"] for e in itin]
    assert "ho-chi-minh-city" in ids
    assert "ho-chi-minh-city-2" in ids


def test_new_hard_entry_with_date_is_held_pending() -> None:
    itin = _itinerary()
    applied, pending = apply_itinerary_changes(
        itin,
        [{"place": "Thailand visa run", "type": "hard", "new_date": "2026-11-01"}],
        on=ON,
    )

    assert applied == []
    assert len(pending) == 1
    change = pending[0]
    assert change.is_new is True
    assert change.old_date is None
    assert change.new_date == "2026-11-01"
    # nothing written to the itinerary yet
    assert len(itin) == 2


def test_existing_soft_entry_status_update_applies_immediately() -> None:
    itin = _itinerary()
    applied, pending = apply_itinerary_changes(
        itin, [{"id": "canggu-bali-id", "status": "done"}], on=ON
    )

    assert pending == []
    entry = find_entry(itin, "canggu-bali-id")
    assert entry is not None
    assert entry["status"] == "done"


def test_existing_soft_entry_window_move_applies_and_logs_slip() -> None:
    itin = _itinerary()
    itin[0]["target_window"] = ["2026-08-01", "2026-09-01"]

    applied, pending = apply_itinerary_changes(
        itin,
        [{"id": "canggu-bali-id", "new_date": "2026-09-15", "reason": "extending"}],
        on=ON,
    )

    assert pending == []
    entry = find_entry(itin, "canggu-bali-id")
    assert entry is not None
    assert entry["target_window"] == ["2026-08-01", date(2026, 9, 15)]
    assert entry["slip_history"] == [
        {"from": date(2026, 9, 1), "to": date(2026, 9, 15), "on": ON, "reason": "extending"}
    ]


def test_existing_hard_entry_date_move_is_held_pending_and_unwritten() -> None:
    itin = _itinerary()
    applied, pending = apply_itinerary_changes(
        itin,
        [{"id": "indonesia-visa-exit", "new_date": "2026-10-15", "reason": "visa expires"}],
        on=ON,
    )

    assert applied == []
    assert len(pending) == 1
    change = pending[0]
    assert change.is_new is False
    assert change.id == "indonesia-visa-exit"
    assert change.new_date == "2026-10-15"

    entry = find_entry(itin, "indonesia-visa-exit")
    assert entry is not None
    assert "deadline" not in entry  # untouched until confirmed


def test_apply_confirmed_change_writes_new_hard_entry() -> None:
    itin = _itinerary()
    _, pending = apply_itinerary_changes(
        itin,
        [{"place": "Thailand visa run", "type": "hard", "new_date": "2026-11-01"}],
        on=ON,
    )
    change = pending[0]

    apply_confirmed_change(itin, change, on=ON)

    entry = find_entry(itin, change.id)
    assert entry is not None
    assert entry["place"] == "Thailand visa run"
    assert entry["deadline"] == date(2026, 11, 1)
    assert entry["slip_history"] == [{"from": None, "to": date(2026, 11, 1), "on": ON}]
    assert len(itin) == 3


def test_apply_confirmed_change_writes_existing_hard_entry() -> None:
    itin = _itinerary()
    _, pending = apply_itinerary_changes(
        itin,
        [{"id": "indonesia-visa-exit", "new_date": "2026-10-15", "reason": "visa expires"}],
        on=ON,
    )
    change = pending[0]

    apply_confirmed_change(itin, change, on=ON)

    entry = find_entry(itin, "indonesia-visa-exit")
    assert entry is not None
    assert entry["deadline"] == date(2026, 10, 15)
    assert entry["slip_history"][0]["reason"] == "visa expires"
    assert len(itin) == 2  # no duplicate entry created


def test_unresolvable_change_with_no_place_is_skipped() -> None:
    itin = _itinerary()
    applied, pending = apply_itinerary_changes(itin, [{"id": "nonexistent"}], on=ON)
    assert applied == [] and pending == []
    assert len(itin) == 2
