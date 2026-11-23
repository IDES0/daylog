"""Measurements from the user's own devices: watch, phone and computer.

One inlet (POST /health/<secret>, in calendar_server) takes three shapes:

- the simple daily object an iOS Shortcut can post
  (`{"date": "2026-10-02", "steps": 8423}`);
- a Health Auto Export payload (`{"data": {"metrics": [...], "workouts":
  [...]}}`), the iOS app that pushes Apple Health / Apple Watch data on a
  schedule: daily metrics, sleep (summarised or as stage segments) and
  workouts with real start and end times;
- screen time: `{"screen": [...]}` sessions from the Mac forwarder
  (tools/screentime, reading ActivityWatch), `{"screen_totals": {...}}`
  per-app daily totals from the iPhone's nightly Shortcut (Screen Time's
  "Get App & Website Data"), and `{"screen_events": [...]}` app opened /
  closed events from iPhone Shortcuts automations, for exact times.

Daily numbers go to `health.yaml` (date -> {metric: number}), as before.
Timed records go to one file per day, `health/days/YYYY-MM-DD.yaml`:
`sleep` (filed under the morning you woke), `workouts` and `screen` (under
the day they started; a screen session crossing midnight is split),
`screen_events` and `screen_totals` (device, app, minutes). Times are kept
as the device's wall clock with its offset ("2026-11-20 06:12:00 -0600"),
so a day abroad reads in local time.

These are measurements beside the journal, not a replacement: the journal
stays the record of what happened and why; the watch says when and how
long. Nothing here is scored.
"""

from __future__ import annotations

import json
import re
import threading
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from daylog.vault import Vault

FILE = "health.yaml"
DAYS_DIR = "health/days"
UNITS_FILE = "health/units.yaml"
MAX_BODY_BYTES = 4096  # the simple daily shape
MAX_EXPORT_BYTES = 8 * 1024 * 1024  # an export or a screen batch
MAX_METRICS = 24
MAX_EXPORT_METRICS = 80
MAX_DAY_SCREEN = 4000
MAX_SESSION_HOURS = 16
_KEY = re.compile(r"^[a-z][a-z0-9_]{0,47}$")
_NUMBER = re.compile(r"-?\d+(?:\.\d+)?")
_STAMP = re.compile(
    r"^(\d{4}-\d{2}-\d{2})[ T](\d{2}):(\d{2})(?::(\d{2}))?(?:\.\d+)?\s*(Z|[+-]\d{2}:?\d{2})?$"
)
_LABEL = re.compile(r"[^\w .&+'()-]")

# Metrics added up over a day; the rest are averaged.
_SUMMED = {
    "step_count",
    "active_energy",
    "basal_energy_burned",
    "walking_running_distance",
    "cycling_distance",
    "swimming_distance",
    "flights_climbed",
    "apple_exercise_time",
    "apple_stand_time",
    "apple_stand_hour",
    "time_in_daylight",
    "mindful_minutes",
    "swimming_stroke_count",
    "number_of_times_fallen",
}
_ASLEEP_STAGES = {"asleep", "core", "deep", "rem", "unspecified"}

KINDS = ("sleep", "workouts", "screen", "screen_events", "screen_totals")

_LOCK = threading.Lock()


class HealthError(ValueError):
    pass


# -- shared helpers ---------------------------------------------------------------


def _number(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int | float):
        return float(value)
    if isinstance(value, str):
        # Shortcuts often sends a number with its unit ("8,423 steps", "1.9 mi").
        match = _NUMBER.search(value.replace(",", ""))
        return float(match.group()) if match else None
    if isinstance(value, dict):
        return _number(value.get("qty"))
    return None


def _day(value: Any, default_day: date) -> date:
    """The posted date, in ISO or a few common phone formats; else the default."""
    text = str(value or "").strip()
    try:
        return date.fromisoformat(text[:10])
    except ValueError:
        pass
    for fmt in ("%d/%m/%Y", "%m/%d/%Y", "%b %d, %Y", "%d %b %Y", "%B %d, %Y"):
        try:
            return datetime.strptime(text.split(" at ")[0], fmt).date()
        except ValueError:
            continue
    return default_day


def stamp(value: Any) -> str | None:
    """A device timestamp, normalised to "YYYY-MM-DD HH:MM:SS ±HHMM" (offset if given).

    Takes Health Auto Export's "2026-11-20 06:12:00 -0600" and ISO 8601
    ("2026-11-20T06:12:00-06:00", "...Z"). The wall clock is kept as the
    device said it, never converted.
    """
    match = _STAMP.match(str(value or "").strip())
    if not match:
        return None
    day, hour, minute, second, offset = match.groups()
    try:
        date.fromisoformat(day)
    except ValueError:
        return None
    if int(hour) > 23 or int(minute) > 59 or int(second or 0) > 59:
        return None
    text = f"{day} {hour}:{minute}:{second or '00'}"
    if offset:
        text += " +0000" if offset == "Z" else " " + offset.replace(":", "")
    return text


def wall(value: Any) -> datetime | None:
    """The wall-clock time of a stored or posted stamp (naive, offset dropped)."""
    text = stamp(value)
    return datetime.fromisoformat(text[:19]) if text else None


def _absolute(value: str) -> datetime:
    """The stamp as an aware instant when it has an offset (for durations)."""
    text = stamp(value) or value
    if len(text) > 19:
        return datetime.strptime(text, "%Y-%m-%d %H:%M:%S %z")
    return datetime.fromisoformat(text[:19])


def _hours_between(start: str, end: str) -> float:
    a, b = _absolute(start), _absolute(end)
    if (a.tzinfo is None) != (b.tzinfo is None):
        a, b = a.replace(tzinfo=None), b.replace(tzinfo=None)
    return (b - a).total_seconds() / 3600


def _label(value: Any, limit: int = 80) -> str:
    return _LABEL.sub("", str(value or "")).strip()[:limit]


def _round(value: float | None, digits: int = 2) -> float | None:
    return None if value is None else round(value, digits)


# -- the simple daily shape -------------------------------------------------------


def skipped(body: bytes, metrics: dict[str, float]) -> list[str]:
    """Posted field names that weren't saved (empty, not a number, or an odd name)."""
    data = json.loads(body)
    names = [str(k).strip().lower().replace(" ", "_") for k in data]
    return [n for n in names if n != "date" and n not in metrics]


def parse(body: bytes, default_day: date) -> tuple[date, dict[str, float]]:
    """Validate a posted simple body into (day, metrics). Raises HealthError."""
    if len(body) > MAX_BODY_BYTES:
        raise HealthError("body too large")
    try:
        data = json.loads(body)
    except (ValueError, UnicodeDecodeError) as exc:
        raise HealthError("not JSON") from exc
    if not isinstance(data, dict):
        raise HealthError("expected a JSON object")

    day = _day(data.get("date"), default_day)

    metrics: dict[str, float] = {}
    for key, value in data.items():
        name = str(key).strip().lower().replace(" ", "_")
        number = None if isinstance(value, dict) else _number(value)
        if name == "date" or number is None or not _KEY.match(name):
            continue
        metrics[name] = round(number, 2)
    if not metrics:
        raise HealthError(f"no numeric metrics in fields: {', '.join(map(str, data)) or 'none'}")
    if len(metrics) > MAX_METRICS:
        raise HealthError("too many metrics")
    return day, metrics


def merge(existing: Any, day: date, metrics: dict[str, float]) -> dict[str, dict[str, float]]:
    """The stored mapping with `day` updated (a later post that day wins per metric)."""
    return merge_days(existing, {day: metrics})


def merge_days(existing: Any, days: dict[date, dict[str, float]]) -> dict[str, dict[str, float]]:
    merged = {str(k): dict(v) for k, v in (existing or {}).items() if isinstance(v, dict)}
    for day, metrics in days.items():
        merged[day.isoformat()] = {**merged.get(day.isoformat(), {}), **metrics}
    return dict(sorted(merged.items()))


def for_day(stored: Any, day: date) -> dict[str, float]:
    row = (stored or {}).get(day.isoformat()) or (stored or {}).get(day)
    return dict(row) if isinstance(row, dict) else {}


# -- exports: Health Auto Export and screen time -----------------------------------


@dataclass
class Batch:
    """What one export post carries, already sorted by day."""

    daily: dict[date, dict[str, float]] = field(default_factory=lambda: defaultdict(dict))
    units: dict[str, str] = field(default_factory=dict)
    days: dict[date, dict[str, list[dict[str, Any]]]] = field(
        default_factory=lambda: defaultdict(lambda: defaultdict(list))
    )
    ignored: list[str] = field(default_factory=list)

    def add(self, day: date, kind: str, record: dict[str, Any]) -> None:
        self.days[day][kind].append({k: v for k, v in record.items() if v not in (None, "")})

    def counts(self) -> dict[str, int]:
        out: dict[str, int] = defaultdict(int)
        for records in self.days.values():
            for kind, items in records.items():
                out[kind] += len(items)
        return dict(out)


def is_export(data: Any) -> bool:
    return isinstance(data, dict) and (
        isinstance(data.get("data"), dict)
        or any(k in data for k in ("screen", "screen_events", "screen_event", "screen_totals"))
    )


def _metric_day(entry: dict[str, Any]) -> date | None:
    moment = wall(entry.get("date"))
    if moment:
        return moment.date()
    try:
        return date.fromisoformat(str(entry.get("date") or "")[:10])
    except ValueError:
        return None


def _metric_name(raw: Any) -> str | None:
    name = str(raw or "").strip().lower().replace(" ", "_").replace("-", "_")
    return name if _KEY.match(name) else None


def _add_metric(batch: Batch, metric: dict[str, Any]) -> None:
    name = _metric_name(metric.get("name"))
    entries: list[Any] = metric["data"] if isinstance(metric.get("data"), list) else []
    if not name:
        batch.ignored.append(str(metric.get("name")))
        return
    if name == "sleep_analysis":
        _add_sleep(batch, [e for e in entries if isinstance(e, dict)])
        return
    if metric.get("units"):
        batch.units[name] = str(metric["units"])[:24]
    values: dict[date, list[float]] = defaultdict(list)
    extra: dict[str, dict[date, list[float]]] = defaultdict(lambda: defaultdict(list))
    for entry in entries:
        if not isinstance(entry, dict) or (day := _metric_day(entry)) is None:
            continue
        if name == "heart_rate" or "Avg" in entry:
            for part in ("Min", "Avg", "Max"):
                if (n := _number(entry.get(part))) is not None:
                    extra[f"{name}_{part.lower()}"][day].append(n)
            continue
        if name == "blood_pressure":
            for part in ("systolic", "diastolic"):
                if (n := _number(entry.get(part))) is not None:
                    extra[f"blood_pressure_{part}"][day].append(n)
            continue
        if (n := _number(entry.get("qty"))) is not None:
            values[day].append(n)
    for day, nums in values.items():
        total = (
            sum(nums) if name in _SUMMED or name.startswith("dietary_") else sum(nums) / len(nums)
        )
        batch.daily[day][name] = round(total, 2)
    for key, by_day in extra.items():
        for day, nums in by_day.items():
            if key.endswith("_min"):
                batch.daily[day][key] = round(min(nums), 2)
            elif key.endswith("_max"):
                batch.daily[day][key] = round(max(nums), 2)
            else:
                batch.daily[day][key] = round(sum(nums) / len(nums), 2)


def _sleep_record(
    start: str, end: str, stages: dict[str, float], in_bed: float | None, source: str
) -> dict[str, Any]:
    asleep = sum(v for k, v in stages.items() if k in _ASLEEP_STAGES)
    return {
        "start": start,
        "end": end,
        "asleep_h": _round(asleep),
        "deep_h": _round(stages.get("deep")),
        "rem_h": _round(stages.get("rem")),
        "core_h": _round(stages.get("core")),
        "awake_h": _round(stages.get("awake")),
        # The phone sends 0 when it has no in-bed data; that's unknown, not zero.
        "in_bed_h": _round(in_bed) if in_bed else None,
        "source": _label(source),
    }


def _add_sleep(batch: Batch, entries: list[dict[str, Any]]) -> None:
    if any("value" in e for e in entries):
        _add_sleep_segments(batch, entries)
        return
    for e in entries:  # summarised: one entry per night
        start = stamp(e.get("sleepStart")) or stamp(e.get("inBedStart"))
        end = stamp(e.get("sleepEnd")) or stamp(e.get("inBedEnd"))
        if not start or not end or _hours_between(start, end) <= 0:
            continue
        stages = {
            k: n
            for k in ("asleep", "core", "deep", "rem", "awake")
            if (n := _number(e.get(k))) is not None
        }
        total = _number(e.get("totalSleep"))
        if total is not None and total > 0:
            # totalSleep is asleep + core + deep + rem; keep it exact.
            stages["asleep"] = max(
                0.0, total - sum(stages.get(k, 0.0) for k in ("core", "deep", "rem"))
            )
        record = _sleep_record(start, end, stages, _number(e.get("inBed")), str(e.get("source")))
        woke = wall(end)
        assert woke is not None
        batch.add(woke.date(), "sleep", record)


def _add_sleep_segments(batch: Batch, entries: list[dict[str, Any]]) -> None:
    """Stage segments (Summarize off) grouped into nights: a 3-hour gap starts a new one."""
    segs = []
    for e in entries:
        start = stamp(e.get("startDate") or e.get("start"))
        end = stamp(e.get("endDate") or e.get("end"))
        value = str(e.get("value") or "").strip().lower().replace(" ", "_")
        if start and end and value and _hours_between(start, end) > 0:
            segs.append((start, end, value, _label(e.get("source"))))
    segs.sort(key=lambda s: _absolute(s[0]).replace(tzinfo=None))
    nights: list[list[tuple[str, str, str, str]]] = []
    for seg in segs:
        if nights and _hours_between(max(s[1] for s in nights[-1]), seg[0]) < 3:
            nights[-1].append(seg)
        else:
            nights.append([seg])
    for night in nights:
        staged = {s[3] for s in night if s[2] in ("core", "deep", "rem")}
        # Phone and watch both report "asleep"; the watch's stages win.
        use = [s for s in night if not staged or s[3] in staged or s[2] == "in_bed"]
        sleeping = [s for s in use if s[2] in _ASLEEP_STAGES]
        if not sleeping:
            continue
        stages: dict[str, float] = defaultdict(float)
        for s in use:
            if s[2] != "in_bed":
                stages[s[2]] += _hours_between(s[0], s[1])
        bed = [s for s in use if s[2] == "in_bed"]
        in_bed = sum(_hours_between(s[0], s[1]) for s in bed) if bed else None
        start = min((s[0] for s in sleeping), key=lambda t: _absolute(t).replace(tzinfo=None))
        end = max((s[1] for s in sleeping), key=lambda t: _absolute(t).replace(tzinfo=None))
        source = sorted(staged)[0] if staged else sleeping[0][3]
        woke = wall(end)
        assert woke is not None
        batch.add(woke.date(), "sleep", _sleep_record(start, end, dict(stages), in_bed, source))


_ENERGY = {"kcal": 1.0, "cal": 1.0, "kj": 1 / 4.184}
_DISTANCE = {"km": 1.0, "m": 0.001, "mi": 1.609344, "yd": 0.0009144, "ft": 0.0003048}
_HEIGHT = {"m": 1.0, "ft": 0.3048}


def _measure(value: Any, table: dict[str, float]) -> float | None:
    """A {qty, units} value in the table's base unit; None for an unknown unit."""
    if not isinstance(value, dict):
        return None
    qty = _number(value.get("qty"))
    factor = table.get(str(value.get("units") or "").strip().lower())
    return None if qty is None or factor is None else qty * factor


def _heart(w: dict[str, Any]) -> tuple[float | None, float | None]:
    hr: dict[str, Any] = w["heartRate"] if isinstance(w.get("heartRate"), dict) else {}
    avg = _number(hr.get("avg")) or _number(w.get("avgHeartRate"))
    peak = _number(hr.get("max")) or _number(w.get("maxHeartRate"))
    series = [p for p in w.get("heartRateData") or [] if isinstance(p, dict)]
    if series and avg is None:
        nums = [n for p in series if (n := _number(p.get("Avg", p.get("qty")))) is not None]
        avg = sum(nums) / len(nums) if nums else None
    if series and peak is None:
        nums = [n for p in series if (n := _number(p.get("Max", p.get("qty")))) is not None]
        peak = max(nums) if nums else None
    return avg, peak


def _add_workout(batch: Batch, w: dict[str, Any]) -> None:
    start, end = stamp(w.get("start")), stamp(w.get("end"))
    name = _label(w.get("name"), 60)
    if not start or not end or not name or _hours_between(start, end) <= 0:
        batch.ignored.append(f"workout {name or '?'}")
        return
    seconds = _number(w.get("duration"))
    minutes = seconds / 60 if seconds else _hours_between(start, end) * 60
    # v2 sends activeEnergyBurned and activeEnergy as a series; v1 a total.
    energy = w.get("activeEnergyBurned")
    if not isinstance(energy, dict):
        energy = w.get("activeEnergy") if isinstance(w.get("activeEnergy"), dict) else None
    avg, peak = _heart(w)
    source = w.get("source")
    began = wall(start)
    assert began is not None
    batch.add(
        began.date(),
        "workouts",
        {
            "id": _label(w.get("id"), 64) or f"{name}@{start}",
            "kind": name,
            "start": start,
            "end": end,
            "minutes": _round(minutes, 1),
            "kcal": _round(_measure(energy, _ENERGY), 0),
            "distance_km": _round(_measure(w.get("distance"), _DISTANCE)),
            "hr_avg": _round(avg, 0),
            "hr_max": _round(peak, 0),
            "elevation_up_m": _round(_measure(w.get("elevationUp"), _HEIGHT), 0),
            "effort": _round(_number(w.get("effortScore")), 1),
            "location": _label(w.get("location"), 20),
            "source": _label(source.get("name") if isinstance(source, dict) else source),
        },
    )


def _split_at_midnight(start: str, end: str) -> list[tuple[str, str]]:
    """A session's pieces, one per calendar day (wall clock)."""
    a, b = wall(start), wall(end)
    assert a is not None and b is not None
    offset = start[19:]
    pieces = []
    while a.date() < b.date():
        midnight = datetime.combine(a.date() + timedelta(days=1), datetime.min.time())
        pieces.append((a, midnight - timedelta(seconds=1)))
        a = midnight
    pieces.append((a, b))
    return [(f"{x:%Y-%m-%d %H:%M:%S}{offset}", f"{y:%Y-%m-%d %H:%M:%S}{offset}") for x, y in pieces]


def _add_screen(batch: Batch, s: dict[str, Any]) -> None:
    start, end = stamp(s.get("start")), stamp(s.get("end"))
    app, device = _label(s.get("app")), _label(s.get("device"), 20).lower() or "mac"
    if not start or not end or not app:
        batch.ignored.append(f"screen {app or '?'}")
        return
    hours = _hours_between(start, end)
    if not 0 < hours <= MAX_SESSION_HOURS:
        batch.ignored.append(f"screen {app}")
        return
    site = _label(s.get("site"), 120).lower()
    for a, b in _split_at_midnight(start, end):
        moment = wall(a)
        assert moment is not None
        batch.add(
            moment.date(),
            "screen",
            {"device": device, "app": app, "site": site, "start": a, "end": b},
        )


def _add_screen_event(batch: Batch, e: dict[str, Any]) -> None:
    at = stamp(e.get("at") or e.get("date"))
    app, event = _label(e.get("app")), str(e.get("event") or "").strip().lower()
    event = {"opened": "open", "closed": "close"}.get(event, event)
    if not at or not app or event not in ("open", "close"):
        batch.ignored.append(f"screen event {app or '?'}")
        return
    moment = wall(at)
    assert moment is not None
    batch.add(
        moment.date(),
        "screen_events",
        {
            "device": _label(e.get("device"), 20).lower() or "iphone",
            "app": app,
            "event": event,
            "at": at,
        },
    )


_DURATION_PART = re.compile(
    r"(\d+(?:\.\d+)?)\s*(h|hr|hrs|hour|hours|m|min|mins|minute|minutes|s|sec|secs|seconds)\b"
)


_TOTAL_LINE = re.compile(r"^(.+?)\s*(?::|\s-|–)\s*(\d.*)$")


def minutes(value: Any) -> float | None:
    """A duration in minutes: 83, "83", "1h 23m", "1 hr 23 min", "1:23" (h:mm), "1:23:45"."""
    if isinstance(value, bool):
        return None
    if isinstance(value, int | float):
        return float(value)
    text = str(value or "").strip().lower()
    if re.fullmatch(r"\d+:\d{2}(:\d{2})?", text):
        parts = [int(p) for p in text.split(":")]
        return parts[0] * 60 + parts[1] + (parts[2] / 60 if len(parts) > 2 else 0)
    found = _DURATION_PART.findall(text)
    if found:
        scale = {"h": 60.0, "m": 1.0, "s": 1 / 60}
        return sum(float(n) * scale[unit[0]] for n, unit in found)
    return _number(text)


def _add_screen_totals(batch: Batch, data: Any, today: date) -> None:
    """Per-app daily totals: {"date", "device", "apps"}, apps being {name: duration},
    [{app, minutes}] or a Shortcut's text, one "App: 1h 23m" per line."""
    if not isinstance(data, dict):
        return
    day = _day(data.get("date"), today)
    device = _label(data.get("device"), 20).lower() or "iphone"
    apps = data.get("apps")
    pairs: list[tuple[Any, Any]] = []
    if isinstance(apps, dict):
        pairs = list(apps.items())
    elif isinstance(apps, list):
        pairs = [
            (a.get("app") or a.get("name"), a.get("minutes", a.get("duration")))
            for a in apps
            if isinstance(a, dict)
        ]
    elif isinstance(apps, str):
        # A Shortcut's text output, one "App: 1h 23m" (or "App - 23 min") per line.
        for line in apps.splitlines():
            match = _TOTAL_LINE.match(line.strip())
            if match:
                pairs.append((match.group(1), match.group(2)))
    for name, value in pairs:
        app, mins = _label(name), minutes(value)
        if app and mins is not None and 0 < mins <= 24 * 60:
            batch.add(
                day, "screen_totals", {"device": device, "app": app, "minutes": round(mins, 1)}
            )
        else:
            batch.ignored.append(f"screen total {app or '?'}")


def parse_export(data: dict[str, Any], today: date | None = None) -> Batch:
    """A Health Auto Export / screen-time payload, sorted into days. Raises HealthError."""
    batch = Batch()
    inner: dict[str, Any] = data["data"] if isinstance(data.get("data"), dict) else {}
    for metric in inner.get("metrics") or []:
        if isinstance(metric, dict):
            _add_metric(batch, metric)
    for workout in inner.get("workouts") or []:
        if isinstance(workout, dict):
            _add_workout(batch, workout)
    for s in data.get("screen") or []:
        if isinstance(s, dict):
            _add_screen(batch, s)
    events = data.get("screen_events") or []
    if isinstance(data.get("screen_event"), dict):
        events = [*events, data["screen_event"]]
    for e in events:
        if isinstance(e, dict):
            _add_screen_event(batch, e)
    totals = data.get("screen_totals")
    for t in totals if isinstance(totals, list) else [totals]:
        _add_screen_totals(batch, t, today or date.today())
    for day, metrics in batch.daily.items():
        if len(metrics) > MAX_EXPORT_METRICS:
            raise HealthError(f"too many metrics for {day}")
    _sleep_totals(batch)
    return batch


def _sleep_totals(batch: Batch) -> None:
    """Each morning's sleep as daily numbers too, for trends and the export."""
    for day, records in batch.days.items():
        nights = records.get("sleep") or []
        if not nights:
            continue
        for key, name in (
            ("asleep_h", "sleep_h"),
            ("deep_h", "sleep_deep_h"),
            ("rem_h", "sleep_rem_h"),
            ("core_h", "sleep_core_h"),
            ("awake_h", "sleep_awake_h"),
            ("in_bed_h", "in_bed_h"),
        ):
            values = [float(n[key]) for n in nights if isinstance(n.get(key), int | float)]
            if values:
                batch.daily[day][name] = round(sum(values), 2)


# -- merging into the per-day files ---------------------------------------------------


def day_file(day: date) -> str:
    return f"{DAYS_DIR}/{day.isoformat()}.yaml"


def _plain(record: Any) -> dict[str, Any]:
    return {str(k): v for k, v in dict(record).items()}


def _sorted(records: list[dict[str, Any]], key: str) -> list[dict[str, Any]]:
    return sorted(records, key=lambda r: wall(r.get(key)) or datetime.min)


def _overlap(a: dict[str, Any], b: dict[str, Any]) -> float:
    """Hours two timed records share (wall clock)."""
    a0, a1, b0, b1 = (
        wall(a.get("start")),
        wall(a.get("end")),
        wall(b.get("start")),
        wall(b.get("end")),
    )
    if not (a0 and a1 and b0 and b1):
        return 0.0
    return max(0.0, (min(a1, b1) - max(a0, b0)).total_seconds() / 3600)


def merge_records(existing: Any, new: dict[str, list[dict[str, Any]]]) -> dict[str, Any]:
    """A day file with a post's records merged in; re-posting the same data changes nothing.

    Workouts are keyed by id; a night of sleep replaces one it mostly
    overlaps (a later export knows the whole night); screen sessions are
    keyed by device, app, site and start (the collector re-sends a session
    that grew); events by all their fields.
    """
    out = {
        str(k): [_plain(r) for r in v]
        for k, v in dict(existing or {}).items()
        if isinstance(v, list)
    }
    for w in new.get("workouts", []):
        kept = [r for r in out.get("workouts", []) if r.get("id") != w["id"]]
        out["workouts"] = _sorted([*kept, w], "start")
    for night in new.get("sleep", []):
        length = _hours_between(night["start"], night["end"])
        kept = [
            r
            for r in out.get("sleep", [])
            if _overlap(r, night) < 0.5 * min(length, _hours_between(r["start"], r["end"]))
        ]
        out["sleep"] = _sorted([*kept, night], "start")
    if new.get("screen"):
        by_key = {
            (r.get("device"), r.get("app"), r.get("site", ""), r.get("start")): r
            for r in out.get("screen", [])
        }
        for s in new["screen"]:
            by_key[(s["device"], s["app"], s.get("site", ""), s["start"])] = s
        out["screen"] = _sorted(list(by_key.values()), "start")[-MAX_DAY_SCREEN:]
    if new.get("screen_events"):
        seen = {tuple(sorted(r.items())) for r in out.get("screen_events", [])}
        events = list(out.get("screen_events", []))
        for e in new["screen_events"]:
            if tuple(sorted(e.items())) not in seen:
                events.append(e)
                seen.add(tuple(sorted(e.items())))
        out["screen_events"] = _sorted(events, "at")[-MAX_DAY_SCREEN:]
    if new.get("screen_totals"):
        # A device's totals for a day are replaced whole: the latest post knows the day best.
        devices = {t["device"] for t in new["screen_totals"]}
        kept = [t for t in out.get("screen_totals", []) if t.get("device") not in devices]
        out["screen_totals"] = sorted(
            [*kept, *new["screen_totals"]],
            key=lambda t: (t.get("device", ""), -float(t.get("minutes") or 0)),
        )
    return {k: out[k] for k in KINDS if out.get(k)}


def read_days(vault: Vault, start: date, end: date) -> dict[date, dict[str, Any]]:
    """The per-day record files for start..end inclusive (days without one are absent)."""
    out: dict[date, dict[str, Any]] = {}
    day = start
    while day <= end:
        data = vault.read_yaml(day_file(day), None)
        if isinstance(data, dict):
            out[day] = {
                str(k): [_plain(r) for r in v] for k, v in data.items() if isinstance(v, list)
            }
        day += timedelta(days=1)
    return out


# -- the inlet ------------------------------------------------------------------------


_NOUNS = {
    "sleep": "night of sleep",
    "workouts": "workout",
    "screen": "screen session",
    "screen_events": "screen event",
    "screen_totals": "app total",
}


def _count(n: int, noun: str) -> str:
    """ "1 workout", "2 screen sessions", "3 nights of sleep"."""
    if n == 1:
        return f"1 {noun}"
    head, of, tail = noun.partition(" of ")
    return f"{n} {head}s{of}{tail}"


def ingest(vault: Vault, body: bytes, today: date) -> str:
    """Store one post to the inlet (any of the three shapes); returns the reply line."""
    if len(body) > MAX_EXPORT_BYTES:
        raise HealthError("body too large")
    try:
        data = json.loads(body)
    except (ValueError, UnicodeDecodeError) as exc:
        raise HealthError("not JSON") from exc
    if not is_export(data):
        day, metrics = parse(body, today)
        with _LOCK:
            stored = merge(vault.read_yaml(FILE, {}), day, metrics)
            vault.write_yaml(FILE, stored, f"health: {day.isoformat()}")
        left_out = skipped(body, metrics)
        note = f"; skipped (no number): {', '.join(left_out)}" if left_out else ""
        return f"saved {', '.join(metrics)} for {day.isoformat()}{note}"

    batch = parse_export(data, today)
    if not batch.daily and not batch.days:
        # A run with nothing new (no workouts this week) or only types daylog
        # doesn't read is fine, not an error: the app would flag every run.
        note = f"; ignored: {', '.join(batch.ignored[:8])}" if batch.ignored else ""
        return f"nothing new to save{note}"
    with _LOCK:
        files: dict[str, Any] = {}
        if batch.daily:
            current = vault.read_yaml(FILE, {})
            stored = merge_days(current, dict(batch.daily))
            if stored != {str(k): dict(v) for k, v in dict(current or {}).items()}:
                files[FILE] = stored
        if batch.units:
            units = dict(vault.read_yaml(UNITS_FILE, {}) or {})
            if any(units.get(k) != v for k, v in batch.units.items()):
                files[UNITS_FILE] = dict(sorted({**units, **batch.units}.items()))
        for day, records in sorted(batch.days.items()):
            current = vault.read_yaml(day_file(day), {})
            merged = merge_records(current, records)
            if merged != merge_records(current, {}):
                files[day_file(day)] = merged
        all_days = sorted({*batch.daily, *batch.days})
        span = all_days[0].isoformat() + (
            f"..{all_days[-1].isoformat()}" if len(all_days) > 1 else ""
        )
        if files:
            vault.write_yaml_many(files, f"health: {span}")
    counts = batch.counts()
    parts = [_count(len(batch.daily), "day") + " of numbers"] if batch.daily else []
    parts += [_count(n, _NOUNS[kind]) for kind, n in sorted(counts.items())]
    note = f"; ignored: {', '.join(batch.ignored[:8])}" if batch.ignored else ""
    changed = "" if files else " (nothing new)"
    return f"saved {', '.join(parts)} for {span}{changed}{note}"
