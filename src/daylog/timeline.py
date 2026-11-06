"""A day's activities laid out on a clock, for the week calendar.

Pure logic. An activity may carry (all optional):

- `at`: its start, "HH:MM";
- `until`: its end, "HH:MM" ("got home around 4");
- `hours`: how long it took (the total, across sessions);
- `sessions`: separate timed sessions of one activity, each
  `{at, until?, hours?}` ("did the 4pm, 6pm and 7pm sessions");
- `time`: `said` when the user stated the time(s), `estimated` when the
  extraction inferred them ("after lunch"). Unmarked times are estimated:
  before 2026-11-06 every `at` was a ballpark.

The logical day runs from the 4am cutoff to the next 4am, so a time before
CUTOFF is after midnight: "02:00" is 26.0, the end of the day, not its start.

Each block is one of three kinds, drawn from firmest to softest:

- `said`: where the user said, exactly;
- `estimated`: where the extraction put it;
- `guessed`: no time at all: after the thing told before it (or before the
  first timed one), moved on to the first free stretch that fits.

A block's length comes from `until`, else its hours (an activity's hours
shared across its sessions), else DEFAULT_HOURS. Overlaps that remain (a
call during a long beach day) are split into side-by-side lanes.

Measured times (a watch workout with a real start and end) would slot in
here later as a fourth kind, firmer than all three.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

CUTOFF = 4.0  # daily.DAY_CUTOFF_HOUR's default: the logical day starts here
DAY_START = CUTOFF
DAY_END = CUTOFF + 24.0
FIRST_GUESS = 8.0
DEFAULT_HOURS = 1.0
MIN_HOURS = 0.5  # shorter is too thin to see or tap
# Logged, but not something you'd put on a calendar.
OFF_CALENDAR = ("meal",)

SAID, ESTIMATED, GUESSED = "said", "estimated", "guessed"
TIME_FIELDS = ("at", "until", "sessions", "time")

_CLOCK = re.compile(r"(\d{1,2}):(\d{2})")


@dataclass
class Block:
    kind: str
    detail: str
    start: float  # hours since midnight; past 24 is after midnight
    end: float
    time: str = ESTIMATED  # SAID | ESTIMATED | GUESSED
    session: str = ""  # "2/3" for the second of three sessions
    lane: int = 0
    lanes: int = 1

    @property
    def guessed(self) -> bool:
        return self.time == GUESSED


def clock(value: Any) -> float | None:
    """'06:30' / '~6:30' -> 6.5; anything else -> None."""
    match = _CLOCK.search(str(value or ""))
    if not match:
        return None
    hour, minute = int(match.group(1)), int(match.group(2))
    if hour > 23 or minute > 59:
        return None
    return hour + minute / 60


def day_clock(value: Any) -> float | None:
    """A clock time on the logical day's axis: before the cutoff is after midnight."""
    t = clock(value)
    if t is None:
        return None
    return t + 24 if t < CUTOFF else t


def hhmm(t: float) -> str:
    """6.5 -> '06:30'; 26.0 -> '02:00'."""
    minutes = round(t * 60) % (24 * 60)
    return f"{minutes // 60:02d}:{minutes % 60:02d}"


def _number(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, int | float) or value <= 0:
        return None
    return float(value)


def _span(
    at: float | None, until: float | None, hours: float | None
) -> tuple[float | None, float | None]:
    """Start and end from whatever was given; (None, None) when nothing places it."""
    if at is not None and until is not None and until > at:
        return at, until
    # An end at or before the start can't be read; the start wins.
    until = None if at is not None else until
    length = max(MIN_HOURS, hours or DEFAULT_HOURS)
    if at is not None:
        return at, at + length
    if until is not None:
        return until - length, until
    return None, None


def _clamp(start: float, end: float) -> tuple[float, float]:
    length = max(MIN_HOURS, end - start)
    start = min(max(start, DAY_START), DAY_END - MIN_HOURS)
    return start, min(start + length, DAY_END)


def _time_kind(item: dict[str, Any]) -> str:
    return SAID if item.get("time") == SAID else ESTIMATED


def _sessions(item: dict[str, Any]) -> list[dict[str, Any]]:
    raw = item.get("sessions")
    if not isinstance(raw, list):
        return []
    return [s for s in raw if isinstance(s, dict) and (clock(s.get("at")) or clock(s.get("until")))]


def _timed(item: dict[str, Any]) -> list[tuple[float, float, str]]:
    """(start, end, session label) for each timed piece of one activity, or []."""
    hours = _number(item.get("hours"))
    sessions = _sessions(item)
    if sessions:
        share = hours / len(sessions) if hours else None
        out = []
        for i, s in enumerate(sessions, 1):
            start, end = _span(
                day_clock(s.get("at")),
                day_clock(s.get("until")),
                _number(s.get("hours")) or share,
            )
            if start is not None and end is not None:
                out.append((*_clamp(start, end), f"{i}/{len(sessions)}"))
        return out
    start, end = _span(day_clock(item.get("at")), day_clock(item.get("until")), hours)
    if start is None or end is None:
        return []
    return [(*_clamp(start, end), "")]


def _free_from(start: float, length: float, taken: list[tuple[float, float]]) -> float:
    """The first start at or after `start` where `length` fits between taken spans."""
    for a, b in sorted(taken):
        if start + length <= a:
            break
        if b > start:
            start = b
    return start


def day_blocks(frontmatter: dict[str, Any]) -> list[Block]:
    items = [
        item
        for item in frontmatter.get("activities") or []
        if isinstance(item, dict) and item.get("type") not in OFF_CALENDAR
    ]
    timed = [_timed(item) for item in items]
    taken = [(s, e) for pieces in timed for s, e, _ in pieces]
    blocks: list[Block] = []
    cursor: float | None = None  # where the thing told before ended
    for i, (item, pieces) in enumerate(zip(items, timed, strict=True)):
        kind, detail = str(item.get("type", "?")), str(item.get("detail") or "")
        if pieces:
            when = _time_kind(item)
            for start, end, label in pieces:
                blocks.append(Block(kind, detail, start, end, when, label))
            cursor = max(e for _, e, _ in pieces)
            continue
        length = max(MIN_HOURS, _number(item.get("hours")) or DEFAULT_HOURS)
        if cursor is None:
            # The first thing told, untimed: just before the next timed thing if
            # there's room, else the first free stretch after the morning guess.
            later = [p[0][0] for p in timed[i + 1 :] if p]
            if (
                later
                and later[0] - length >= FIRST_GUESS - 1
                and not _overlaps(later[0] - length, later[0], taken)
            ):
                start = later[0] - length
            else:
                start = _free_from(FIRST_GUESS, length, taken)
        else:
            start = _free_from(cursor, length, taken)
        start, end = _clamp(start, start + length)
        blocks.append(Block(kind, detail, start, end, GUESSED))
        taken.append((start, end))
        cursor = end
    _lay_out_lanes(blocks)
    return blocks


def _overlaps(start: float, end: float, taken: list[tuple[float, float]]) -> bool:
    return any(a < end and start < b for a, b in taken)


def _lay_out_lanes(blocks: list[Block]) -> None:
    """Side-by-side lanes for overlapping blocks, per cluster of overlaps."""
    ordered = sorted(blocks, key=lambda b: (b.start, b.end))
    cluster: list[Block] = []
    lane_ends: list[float] = []
    cluster_end = -1.0

    def close() -> None:
        for b in cluster:
            b.lanes = len(lane_ends)

    for b in ordered:
        if cluster and b.start >= cluster_end:
            close()
            cluster, lane_ends = [], []
        for i, lane_end in enumerate(lane_ends):
            if lane_end <= b.start:
                b.lane, lane_ends[i] = i, b.end
                break
        else:
            b.lane = len(lane_ends)
            lane_ends.append(b.end)
        cluster.append(b)
        cluster_end = max(cluster_end, b.end)
    if cluster:
        close()


# -- tidying what the extraction wrote ------------------------------------------------


def tidy_times(activity: dict[str, Any]) -> None:
    """Normalise one extracted activity's time fields in place: clock strings as
    'HH:MM', sessions kept only when they carry a time, `time` only said or
    estimated, the first session's start mirrored to `at`. Drops what can't be read."""
    for key in ("at", "until"):
        if key in activity:
            t = clock(activity[key])
            if t is None:
                del activity[key]
            else:
                activity[key] = hhmm(t)
    if "sessions" in activity:
        tidy: list[dict[str, Any]] = []
        for s in activity["sessions"] if isinstance(activity["sessions"], list) else []:
            if not isinstance(s, dict):
                continue
            one: dict[str, Any] = {}
            for key in ("at", "until"):
                t = clock(s.get(key))
                if t is not None:
                    one[key] = hhmm(t)
            if _number(s.get("hours")):
                one["hours"] = s["hours"]
            if "at" in one or "until" in one:
                tidy.append(one)
        if len(tidy) > 1:
            activity["sessions"] = tidy
            first = tidy[0].get("at")
            if first:
                activity["at"] = first
        else:
            del activity["sessions"]
            if tidy:  # one session is just the activity's own time
                for key, value in tidy[0].items():
                    activity.setdefault(key, value)
    if activity.get("time") not in (SAID, ESTIMATED) or not (
        "at" in activity or "until" in activity
    ):
        activity.pop("time", None)


def keep_said_times(old: list[Any] | None, new: list[Any] | None) -> int:
    """Carry times the user stated from a day's old record onto its rebuilt one.

    The nightly reconcile re-extracts the whole day; when it comes back with a
    softer time (or none) for an activity whose time was said, keep the said
    one. Matched by type, in order. Returns how many were carried."""
    spare = [a for a in old or [] if isinstance(a, dict) and a.get("time") == SAID]
    carried = 0
    for activity in new or []:
        if not isinstance(activity, dict) or activity.get("time") == SAID:
            continue
        match = next((a for a in spare if a.get("type") == activity.get("type")), None)
        if match is None:
            continue
        spare.remove(match)
        for key in TIME_FIELDS:
            activity.pop(key, None)
            if key in match:
                activity[key] = match[key]
        if activity.get("hours") is None and match.get("hours") is not None:
            activity["hours"] = match["hours"]
        carried += 1
    return carried
