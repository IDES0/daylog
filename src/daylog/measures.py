"""Views derived from the measured day files (health/days/*.yaml), never stored.

Pure logic over what health.py wrote:

- `screen_sessions`: a day's screen use as sessions — the Mac forwarder's
  sessions as sent, plus iPhone "app opened / closed" events paired up;
- `screen_blocks`: those sessions coalesced into calendar-sized stretches
  ("Mac · 2 h", mostly Code), since a block per app switch is noise;
- `screen_minutes`: minutes per device and per app, from the phone's
  nightly per-app totals where sent, else from timed sessions;
- `match_workouts`: which watch workout is which journal activity, so the
  calendar can draw the activity at its measured time.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Any

from daylog.disciplines import Graph, slug
from daylog.health import wall

# An unclosed "opened" counts until the next event, at most this long.
OPEN_EVENT_CAP = timedelta(minutes=30)
# Screen sessions closer than this join into one calendar block.
SCREEN_GAP = timedelta(minutes=10)
SCREEN_MIN_BLOCK = timedelta(minutes=10)
# A workout and a told activity this far apart in time aren't the same thing.
MATCH_WINDOW_HOURS = 2.0

# Apple's workout names -> the words a journal would use for the same thing.
# Matched through the discipline graph, so "Surfing" fits a `surf` activity
# (or one under it); names not listed are tried as they are.
WORKOUT_LABELS: dict[str, tuple[str, ...]] = {
    "surfing": ("surf", "surfing"),
    "surfing sports": ("surf", "surfing"),
    "swimming": ("swim", "swimming"),
    "pool swim": ("swim", "swimming"),
    "open water swim": ("swim", "swimming"),
    "hiking": ("hike", "hiking"),
    "walking": ("walk", "walking"),
    "running": ("run", "running"),
    "cycling": ("bike", "cycling", "cycle"),
    "climbing": ("climb", "climbing"),
    "yoga": ("yoga",),
    "snowboarding": ("snowboard", "snowboarding"),
    "downhill skiing": ("ski", "skiing"),
    "cross country skiing": ("ski", "skiing"),
    "skating": ("skate", "skateboarding"),
    "skating sports": ("skate", "skateboarding"),
    "paddle sports": ("paddle", "sup", "paddleboarding"),
    "paddling": ("paddle", "sup", "paddleboarding"),
    "sailing": ("sail", "sailing"),
    "rowing": ("row", "rowing"),
    "traditional strength training": ("gym", "strength"),
    "functional strength training": ("gym", "strength"),
    "core training": ("gym", "core"),
    "high intensity interval training": ("hiit", "gym"),
    "pilates": ("pilates",),
    "dance": ("dance",),
    "boxing": ("boxing",),
    "kickboxing": ("kickboxing", "boxing"),
    "martial arts": ("martial_arts",),
    "underwater diving": ("dive", "freedive", "scuba", "diving"),
    "snow sports": ("snow", "snowboard", "ski"),
}
# Shorter than this is an accidental start (a 1-minute "surf" at 1am), not a session.
MIN_WORKOUT_MINUTES = 3.0
# Same-kind workouts this close are one session told in pieces (a Surfline
# session saved as two after a pause), joined before matching.
JOIN_GAP_MINUTES = 15.0

# Workouts that say nothing about the sport: matched by time alone.
GENERIC_WORKOUTS = {"other", "water sports", "mixed cardio", "cross training", "water fitness"}


@dataclass
class Session:
    device: str
    app: str
    site: str
    start: datetime  # wall clock
    end: datetime

    @property
    def minutes(self) -> float:
        return (self.end - self.start).total_seconds() / 60


@dataclass
class ScreenBlock:
    device: str
    start: datetime
    end: datetime
    apps: dict[str, float] = field(default_factory=dict)  # app or site -> minutes

    @property
    def top(self) -> str:
        return max(self.apps.items(), key=lambda kv: kv[1])[0] if self.apps else ""


def _session(record: dict[str, Any]) -> Session | None:
    start, end = wall(record.get("start")), wall(record.get("end"))
    if not start or not end or end <= start:
        return None
    return Session(
        device=str(record.get("device") or "mac"),
        app=str(record.get("app") or "?"),
        site=str(record.get("site") or ""),
        start=start,
        end=end,
    )


def paired_events(events: list[dict[str, Any]]) -> list[Session]:
    """iPhone opened/closed events as sessions: each open runs to its app's close,
    or to the next event, capped at OPEN_EVENT_CAP."""
    parsed = sorted(
        (
            (at, str(e.get("device") or "iphone"), str(e.get("app") or "?"), str(e.get("event")))
            for e in events
            if (at := wall(e.get("at")))
        ),
        key=lambda x: x[0],
    )
    out: list[Session] = []
    for i, (at, device, app, event) in enumerate(parsed):
        if event != "open":
            continue
        end = at + OPEN_EVENT_CAP
        for later, dev2, app2, event2 in parsed[i + 1 :]:
            if dev2 != device:
                continue
            if event2 == "close" and app2 == app:
                end = min(end, later)
                break
            if event2 == "open":
                end = min(end, later)
                break
        if end > at:
            out.append(Session(device, app, "", at, end))
    return out


def screen_sessions(records: dict[str, Any] | None) -> list[Session]:
    records = records or {}
    sessions = [s for r in records.get("screen") or [] if (s := _session(r))]
    sessions += paired_events(records.get("screen_events") or [])
    return sorted(sessions, key=lambda s: s.start)


def screen_blocks(sessions: list[Session]) -> list[ScreenBlock]:
    """Sessions per device joined across gaps under SCREEN_GAP; short stretches dropped."""
    blocks: list[ScreenBlock] = []
    open_by_device: dict[str, ScreenBlock] = {}
    for s in sorted(sessions, key=lambda s: s.start):
        current = open_by_device.get(s.device)
        if current is None or s.start - current.end > SCREEN_GAP:
            current = ScreenBlock(s.device, s.start, s.end)
            open_by_device[s.device] = current
            blocks.append(current)
        current.end = max(current.end, s.end)
        name = s.site or s.app
        current.apps[name] = current.apps.get(name, 0.0) + s.minutes
    return [b for b in blocks if b.end - b.start >= SCREEN_MIN_BLOCK]


def screen_minutes(records: dict[str, Any] | None) -> dict[str, dict[str, float]]:
    """{device: {app or site: minutes}} for a day. A device that sent daily totals
    (the phone) is counted from them; one with only timed sessions (the Mac) from those."""
    records = records or {}
    out: dict[str, dict[str, float]] = defaultdict(lambda: defaultdict(float))
    totals = [t for t in records.get("screen_totals") or [] if isinstance(t, dict)]
    with_totals = {str(t.get("device") or "iphone") for t in totals}
    for t in totals:
        minutes = t.get("minutes")
        if isinstance(minutes, int | float) and not isinstance(minutes, bool):
            out[str(t.get("device") or "iphone")][str(t.get("app") or "?")] += float(minutes)
    for s in screen_sessions(records):
        # A device's daily totals cover every app; its few timed sessions would double count.
        if s.device not in with_totals:
            out[s.device][s.site or s.app] += s.minutes
    return {
        d: {a: round(m, 1) for a, m in sorted(apps.items(), key=lambda kv: -kv[1])}
        for d, apps in out.items()
    }


def sessions(workouts: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """The day's workouts as sessions: accidental blips dropped, and same-kind
    pieces less than JOIN_GAP_MINUTES apart joined (minutes and kcal added,
    the higher max heart rate kept, the average weighted by minutes)."""
    timed, untimed = [], []
    for w in workouts:
        start, end = wall(w.get("start")), wall(w.get("end"))
        if not start or not end:
            untimed.append(dict(w))  # nothing to join or measure: kept as it came
        elif (end - start).total_seconds() >= MIN_WORKOUT_MINUTES * 60:
            timed.append((start, end, dict(w)))
    timed.sort(key=lambda t: t[0])
    out: list[tuple[datetime, datetime, dict[str, Any]]] = []
    for start, end, w in timed:
        if out:
            p_start, p_end, prev = out[-1]
            same = str(prev.get("kind")).lower() == str(w.get("kind")).lower()
            if same and (start - p_end).total_seconds() <= JOIN_GAP_MINUTES * 60:
                out[-1] = (p_start, max(p_end, end), _join(prev, w, end))
                continue
        out.append((start, end, w))
    return [w for _, _, w in out] + untimed


def _join(a: dict[str, Any], b: dict[str, Any], end: datetime) -> dict[str, Any]:
    def num(w: dict[str, Any], key: str) -> float:
        value = w.get(key)
        return (
            float(value) if isinstance(value, int | float) and not isinstance(value, bool) else 0.0
        )

    joined = dict(a)
    joined["end"] = (
        b["end"] if (wall(b.get("end")) or end) >= (wall(a.get("end")) or end) else a["end"]
    )
    joined["pieces"] = int(a.get("pieces") or 1) + 1
    for key in ("minutes", "kcal", "distance_km"):
        if a.get(key) is not None or b.get(key) is not None:
            joined[key] = round(num(a, key) + num(b, key), 2)
    if a.get("hr_max") is not None or b.get("hr_max") is not None:
        joined["hr_max"] = max(num(a, "hr_max"), num(b, "hr_max"))
    ma, mb = num(a, "minutes"), num(b, "minutes")
    if a.get("hr_avg") is not None and b.get("hr_avg") is not None and ma + mb > 0:
        joined["hr_avg"] = round((num(a, "hr_avg") * ma + num(b, "hr_avg") * mb) / (ma + mb))
    return joined


def _labels(kind: str) -> tuple[str, ...]:
    key = kind.strip().lower()
    if key in WORKOUT_LABELS:
        return WORKOUT_LABELS[key]
    base = key.removesuffix(" sports").removesuffix(" sport")  # Apple's "Paddle Sports"
    return WORKOUT_LABELS.get(base, (key, slug(key), base, slug(base)))


def same_sport(graph: Graph | None, activity_type: str, workout_kind: str) -> bool:
    """Whether a journal activity type is the workout's sport (or a kind of it)."""
    kind = workout_kind.strip().lower()
    if kind in GENERIC_WORKOUTS:
        return False
    a = activity_type.strip().lower()
    for label in _labels(kind):
        if a == label or a == slug(label):
            return True
        if graph:
            node = graph.resolve(label)
            mine = graph.resolve(a)
            if node and mine and node in graph.lineage(mine):
                return True
    return False


def _gap_hours(a0: float, a1: float, b0: float, b1: float) -> float:
    """0 when two spans overlap, else the hours between them."""
    return max(0.0, max(a0, b0) - min(a1, b1))


def clock_hours(moment: datetime, day: date) -> float:
    """Hours since `day`'s midnight (negative before it, over 24 after)."""
    return (moment - datetime.combine(day, datetime.min.time())).total_seconds() / 3600


def match_workouts(
    activities: list[tuple[int, str, float | None, float]],
    workouts: list[dict[str, Any]],
    graph: Graph | None,
    day: date,
) -> dict[int, dict[str, Any]]:
    """{activity index: workout} for the day.

    `activities` are (index, type, estimated start or None, hours). A workout
    goes to the told activity of the same sport nearest in time (within
    MATCH_WINDOW_HOURS when the activity has an estimated time); a generic
    workout ("Other", "Water Sports") only to a sport activity overlapping it.
    Each pairs at most once.
    """
    taken: set[int] = set()
    out: dict[int, dict[str, Any]] = {}
    timed = []
    for w in workouts:
        start, end = wall(w.get("start")), wall(w.get("end"))
        if start and end:
            timed.append((clock_hours(start, day), clock_hours(end, day), w))
    for w0, w1, w in sorted(timed, key=lambda t: t[0]):
        kind = str(w.get("kind") or "")
        best: tuple[float, int] | None = None
        for index, a_type, at, hours in activities:
            if index in taken:
                continue
            if same_sport(graph, a_type, kind):
                gap = 0.0 if at is None else _gap_hours(at, at + hours, w0, w1)
                if at is not None and gap > MATCH_WINDOW_HOURS:
                    continue
            elif kind.strip().lower() in GENERIC_WORKOUTS and graph and graph.resolve(a_type):
                if at is None or _gap_hours(at, at + hours, w0, w1) > 0.5:
                    continue
                gap = _gap_hours(at, at + hours, w0, w1)
            else:
                continue
            if best is None or gap < best[0]:
                best = (gap, index)
        if best is not None:
            taken.add(best[1])
            out[best[1]] = w
    return out
