"""Where the user has been — derived, never stored.

Two sources, both already in the vault: location.yaml says where the user
was based (a stay, a multi-day trip, a transit day), and each journal
entry's activities/meals say which places they went to that day. A
`visited` flag on a place would drift the way the old `current: true` did;
computing it every time can't.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Any

from daylog.places import PlaceIndex

# Journal frontmatter lists whose items can carry a `place` id.
PLACE_LINKED_FIELDS = ("activities", "meals")


@dataclass
class Stay:
    start: date
    end: date | None  # None = still there
    place: str
    place_id: str | None
    mode: str
    notes: str | None


@dataclass
class TrailDay:
    day: date
    stay: Stay | None
    place_ids: list[str] = field(default_factory=list)
    summary: str = ""


def _as_date(value: Any) -> date | None:
    if value is None:
        return None
    if isinstance(value, date):
        return value
    return date.fromisoformat(str(value))


def stays(location_data: Any) -> list[Stay]:
    out = []
    for entry in location_data or []:
        start = _as_date(entry.get("from"))
        if start is None:
            continue
        out.append(
            Stay(
                start=start,
                end=_as_date(entry.get("to")),
                place=str(entry.get("place", "?")),
                place_id=entry.get("place_id"),
                mode=str(entry.get("mode", "stay")),
                notes=entry.get("notes"),
            )
        )
    return sorted(out, key=lambda s: s.start)


def stay_on(all_stays: list[Stay], day: date) -> Stay | None:
    """The stay covering `day`. A stay's end date is the travel day, which
    belongs to the next stay — so `end` is exclusive, unless nothing follows."""
    match = None
    for stay in all_stays:
        if stay.start <= day and (stay.end is None or day < stay.end):
            match = stay
    return match


def day_place_ids(frontmatter: dict[str, Any]) -> list[str]:
    ids: list[str] = []
    for field_name in PLACE_LINKED_FIELDS:
        for item in frontmatter.get(field_name) or []:
            if not isinstance(item, dict):
                continue
            place_id = item.get("place")
            if place_id and place_id not in ids:
                ids.append(str(place_id))
    return ids


def visits(
    journal: dict[date, dict[str, Any]], index: PlaceIndex | None = None
) -> dict[str, list[date]]:
    """place id -> dates it appears in the journal. With an index, a visit to
    a spot also counts for every area above it (Lakey Peak -> Lakey -> Sumbawa)."""
    out: dict[str, list[date]] = {}
    for day in sorted(journal):
        ids = day_place_ids(journal[day])
        expanded = list(ids)
        if index is not None:
            for place_id in ids:
                expanded += [a["id"] for a in index.ancestors(place_id)]
        for place_id in dict.fromkeys(expanded):
            out.setdefault(place_id, []).append(day)
    return out


def build(
    location_data: Any,
    journal: dict[date, dict[str, Any]],
    summaries: dict[date, str],
    start: date,
    end: date,
) -> list[TrailDay]:
    all_stays = stays(location_data)
    days = []
    day = start
    while day <= end:
        fm = journal.get(day, {})
        days.append(
            TrailDay(
                day=day,
                stay=stay_on(all_stays, day),
                place_ids=day_place_ids(fm),
                summary=summaries.get(day, ""),
            )
        )
        day += timedelta(days=1)
    return days


def _short(d: date) -> str:
    return f"{d.strftime('%b')} {d.day}"


def format_trail(days: list[TrailDay], index: PlaceIndex) -> str:
    """Grouped by stay: a header per base, then one line per day that has
    linked places. Days with nothing linked are folded into the header."""
    if not days:
        return "No trail yet."
    blocks: list[str] = []
    current_key: tuple[Any, ...] | None = None
    block: list[str] = []
    for d in days:
        stay = d.stay
        key = (stay.start, stay.place) if stay else None
        if key != current_key:
            if block:
                blocks.append("\n".join(block))
            current_key = key
            if stay is None:
                block = ["(no location recorded)"]
            else:
                until = _short(stay.end) if stay.end else "now"
                mode = f" [{stay.mode}]" if stay.mode != "stay" else ""
                block = [f"{_short(stay.start)}–{until} · {stay.place}{mode}"]
        if d.place_ids:
            names = []
            for place_id in d.place_ids:
                node = index.get(place_id)
                names.append(f"{node.get('name')}" if node else place_id)
            block.append(f"  {_short(d.day)}: {', '.join(names)}")
    if block:
        blocks.append("\n".join(block))
    return "\n\n".join(blocks)
