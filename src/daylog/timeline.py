"""A day's activities laid out on a clock, roughly, for the week calendar.

Pure logic. Each activity may carry `at` (a rough start the extraction
estimated, "HH:MM") and `hours`. Placed in the order told:

- with `at`: there, marked `estimated` (it was always a ballpark);
- without: right after the previous block (or at FIRST_GUESS for the
  first), marked `guessed`, so the calendar can draw it fainter.

A block without hours gets DEFAULT_HOURS. Overlaps are allowed (a call
during a long beach day) and split into side-by-side lanes.

Measured times (a watch workout, a GPS track) would slot in here later as
a third kind, preferred over both.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

DAY_START = 5.0
DAY_END = 24.0
FIRST_GUESS = 8.0
DEFAULT_HOURS = 1.0
MIN_HOURS = 0.5
# Logged, but not something you'd put on a calendar.
OFF_CALENDAR = ("meal",)

_CLOCK = re.compile(r"(\d{1,2}):(\d{2})")


@dataclass
class Block:
    kind: str
    detail: str
    start: float  # hours since midnight
    end: float
    guessed: bool
    lane: int = 0
    lanes: int = 1


def clock(value: Any) -> float | None:
    """'06:30' / '~6:30' -> 6.5; anything else -> None."""
    match = _CLOCK.search(str(value or ""))
    if not match:
        return None
    hour, minute = int(match.group(1)), int(match.group(2))
    if hour > 23 or minute > 59:
        return None
    return hour + minute / 60


def _hours(value: Any) -> float:
    if isinstance(value, bool) or not isinstance(value, int | float) or value <= 0:
        return DEFAULT_HOURS
    return max(MIN_HOURS, float(value))


def day_blocks(frontmatter: dict[str, Any]) -> list[Block]:
    blocks: list[Block] = []
    cursor: float | None = None
    for item in frontmatter.get("activities") or []:
        if not isinstance(item, dict) or item.get("type") in OFF_CALENDAR:
            continue
        at = clock(item.get("at"))
        start = at if at is not None else (cursor if cursor is not None else FIRST_GUESS)
        start = min(max(start, DAY_START), DAY_END - MIN_HOURS)
        end = min(start + _hours(item.get("hours")), DAY_END)
        blocks.append(
            Block(
                kind=str(item.get("type", "?")),
                detail=str(item.get("detail") or ""),
                start=start,
                end=end,
                guessed=at is None,
            )
        )
        cursor = end
    _lay_out_lanes(blocks)
    return blocks


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
