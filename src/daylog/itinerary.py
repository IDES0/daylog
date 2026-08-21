"""Applying extracted itinerary facts to the live itinerary.yaml structure.

Mirrors goals.py closely: pure logic, no filesystem or git access (vault.py
owns that). Uses the same target_window/deadline/slip_history shape as
goals.yaml, for the same reason a goal's deadline does — a hard date (a
visa expiry, a firm commitment) is never created or moved silently. It's
returned as a PendingChange for the caller to confirm with the user first,
then applied via apply_confirmed_change once they do. Soft entries
(candidate/planned legs) and status/notes-only edits on any entry apply
immediately — the "flexible calendar" is meant to be cheap to update.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date
from typing import Any


@dataclass
class AppliedChange:
    id: str
    place: str
    summary: str


@dataclass
class PendingChange:
    id: str
    place: str
    is_new: bool
    old_date: str | None
    new_date: str
    reason: str | None
    status: str | None
    notes: str | None


def find_entry(itinerary: list[Any], entry_id: str) -> Any | None:
    for entry in itinerary:
        if entry.get("id") == entry_id:
            return entry
    return None


def _slugify(place: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", place.lower()).strip("-") or "place"


def _unique_id(itinerary: list[Any], place: str) -> str:
    base = _slugify(place)
    existing = {e.get("id") for e in itinerary}
    if base not in existing:
        return base
    n = 2
    while f"{base}-{n}" in existing:
        n += 1
    return f"{base}-{n}"


def current_date(entry: Any) -> str | None:
    """The entry's current target date, whichever field holds it (deadline or target_window).

    An open-ended window (e.g. [start, null] — "still here, no end date
    yet") has a real end slot that's None, not a missing one — return None
    rather than the literal string "None".
    """
    if "deadline" in entry:
        return str(entry["deadline"])
    window = entry.get("target_window")
    if window and window[-1] is not None:
        return str(window[-1])
    return None


def _write_date(
    entry: Any, old_date: str | None, new_date: str, reason: str | None, on: date
) -> None:
    """Mutate `entry` in place: set its date and append a slip_history entry.

    Dates are stored as real `date` objects, not strings — see goals.py's
    _write_slip for why (ruamel quotes a plain date-like string on dump,
    unlike a hand-typed unquoted date, which loads as an actual `date`).
    """
    new_date_value = date.fromisoformat(new_date)
    if entry.get("type") == "hard":
        entry["deadline"] = new_date_value
    else:
        window = entry.get("target_window")
        if window:
            window[-1] = new_date_value
        else:
            entry["target_window"] = [new_date_value, new_date_value]

    history = entry.get("slip_history")
    if history is None:
        history = []
        entry["slip_history"] = history
    record: dict[str, Any] = {
        "from": date.fromisoformat(old_date) if old_date else None,
        "to": new_date_value,
        "on": on,
    }
    if reason:
        record["reason"] = reason
    history.append(record)


def _apply_non_date_fields(entry: Any, change: dict[str, Any]) -> None:
    if change.get("status"):
        entry["status"] = change["status"]
    if change.get("notes"):
        entry["notes"] = change["notes"]


def apply_itinerary_changes(
    itinerary: list[Any], changes: list[dict[str, Any]], on: date
) -> tuple[list[AppliedChange], list[PendingChange]]:
    """Apply extracted itinerary_changes, splitting out ones that need confirmation.

    Each change may reference an existing entry by `id`, or omit `id` to add
    a new place. A hard entry's date (new or moved) is held pending rather
    than applied — everything else (new soft entries, status/notes, soft
    window moves) applies immediately, in place.
    """
    applied: list[AppliedChange] = []
    pending: list[PendingChange] = []

    for change in changes:
        entry_id = change.get("id")
        entry = find_entry(itinerary, entry_id) if entry_id else None
        place = change.get("place") or (entry.get("place") if entry else None)
        if not place:
            continue

        entry_type = (entry.get("type") if entry else None) or change.get("type") or "soft"
        new_date = change.get("new_date")
        old_date = current_date(entry) if entry else None
        date_changing = bool(new_date) and new_date != old_date

        if entry_type == "hard" and date_changing:
            assert new_date is not None
            pending.append(
                PendingChange(
                    id=entry["id"] if entry else _unique_id(itinerary, place),
                    place=place,
                    is_new=entry is None,
                    old_date=old_date,
                    new_date=new_date,
                    reason=change.get("reason"),
                    status=change.get("status"),
                    notes=change.get("notes"),
                )
            )
            continue

        if entry is None:
            entry = {
                "id": _unique_id(itinerary, place),
                "place": place,
                "type": entry_type,
                "status": change.get("status") or "candidate",
            }
            itinerary.append(entry)

        _apply_non_date_fields(entry, change)
        if date_changing:
            assert new_date is not None
            _write_date(entry, old_date, new_date, change.get("reason"), on)

        summary = f"{place}: {entry.get('status', 'candidate')}"
        if date_changing:
            summary += f" -> {new_date}"
        applied.append(AppliedChange(id=entry["id"], place=place, summary=summary))

    return applied, pending


def apply_confirmed_change(itinerary: list[Any], change: PendingChange, on: date) -> None:
    """Apply a hard itinerary change after the user confirms it in Telegram."""
    entry = find_entry(itinerary, change.id)
    if entry is None:
        entry = {"id": change.id, "place": change.place, "type": "hard"}
        itinerary.append(entry)
    if change.status:
        entry["status"] = change.status
    if change.notes:
        entry["notes"] = change.notes
    _write_date(entry, change.old_date, change.new_date, change.reason, on)
