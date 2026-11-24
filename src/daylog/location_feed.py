"""Where the user actually was: places visited, from the phone's location.

A background logger on the iPhone posts points to `POST /location/<secret>`
(the same secret as the health inlet): Overland (recommended, open source,
batches points) or OwnTracks in HTTP mode. This module turns points into
**visits** — a stay of at least MIN_STAY within STAY_RADIUS — and keeps only
those in the vault, one file per day, `location/days/YYYY-MM-DD.yaml`:

    visits:
    - start: "2026-11-23 15:40:00 -0600"
      end: "2026-11-23 19:10:00 -0600"
      lat: 31.5493
      lon: -97.1467
      place: waco-surf          # a known place within PLACE_RADIUS, if any
      name: Waco Surf

Raw points never reach the vault (it's a git repo on GitHub, and a
minute-by-minute map of someone's movements is more than the calendar
needs): the stay still in progress lives in the spool's state (uncommitted,
on the server's disk) until the user moves on, then becomes a visit. A stay
over midnight is split per day. Coordinates are rounded to ~10 m.

This doesn't move `location.yaml` (the base camp): that still changes only
when the user says so. The visits feed the calendar, the feed, chat and
travel times.
"""

from __future__ import annotations

import json
import math
import threading
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta, tzinfo
from typing import TYPE_CHECKING, Any

from daylog import health

if TYPE_CHECKING:
    from daylog.places import PlaceIndex
    from daylog.vault import Vault

DAYS_DIR = "location/days"
STATE = "location_stay"
MAX_BODY_BYTES = 2 * 1024 * 1024
STAY_RADIUS_M = 150.0
MIN_STAY = timedelta(minutes=10)
PLACE_RADIUS_M = 250.0
MAX_ACCURACY_M = 200.0
# A stay this long without a point closes anyway (the phone was off).
MAX_SILENCE = timedelta(hours=18)

_LOCK = threading.Lock()


class LocationError(ValueError):
    pass


@dataclass
class Point:
    at: datetime  # aware
    lat: float
    lon: float


def distance_m(a_lat: float, a_lon: float, b_lat: float, b_lon: float) -> float:
    r = 6371000.0
    p1, p2 = math.radians(a_lat), math.radians(b_lat)
    dp, dl = p2 - p1, math.radians(b_lon - a_lon)
    h = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(math.sqrt(h))


def _num(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    return float(value)


def _when(value: Any) -> datetime | None:
    if isinstance(value, int | float) and not isinstance(value, bool):
        return datetime.fromtimestamp(float(value), UTC)
    text = str(value or "").strip()
    if not text:
        return None
    try:
        moment = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    return moment if moment.tzinfo else moment.replace(tzinfo=UTC)


def parse(data: Any) -> list[Point]:
    """Points from an Overland batch ({"locations": [GeoJSON features]}) or
    OwnTracks messages ({"_type": "location", ...} or a list of them).
    Inaccurate fixes are dropped."""
    points: list[Point] = []
    if isinstance(data, dict) and isinstance(data.get("locations"), list):
        for f in data["locations"]:
            geometry = f.get("geometry") if isinstance(f, dict) else None
            props = f.get("properties") if isinstance(f, dict) else None
            coords = geometry.get("coordinates") if isinstance(geometry, dict) else None
            if not isinstance(props, dict) or not isinstance(coords, list) or len(coords) < 2:
                continue
            acc = _num(props.get("horizontal_accuracy"))
            at, lon, lat = _when(props.get("timestamp")), _num(coords[0]), _num(coords[1])
            if (
                at
                and lat is not None
                and lon is not None
                and (acc is None or acc <= MAX_ACCURACY_M)
            ):
                points.append(Point(at, lat, lon))
        return sorted(points, key=lambda p: p.at)
    messages = data if isinstance(data, list) else [data]
    for m in messages:
        if not isinstance(m, dict) or m.get("_type") != "location":
            continue
        acc = _num(m.get("acc"))
        at, lat, lon = _when(m.get("tst")), _num(m.get("lat")), _num(m.get("lon"))
        if at and lat is not None and lon is not None and (acc is None or acc <= MAX_ACCURACY_M):
            points.append(Point(at, lat, lon))
    return sorted(points, key=lambda p: p.at)


def _centre(points: list[Point]) -> tuple[float, float]:
    return (
        sum(p.lat for p in points) / len(points),
        sum(p.lon for p in points) / len(points),
    )


def stays(
    open_points: list[Point], new: list[Point]
) -> tuple[list[tuple[datetime, datetime, float, float]], list[Point]]:
    """Closed stays (start, end, lat, lon) and the points of the stay still open.

    A stay is consecutive points within STAY_RADIUS_M of their running centre,
    kept when it lasts MIN_STAY. A point outside it closes the stay; the last
    one stays open until the user moves (or MAX_SILENCE passes)."""
    closed: list[tuple[datetime, datetime, float, float]] = []
    cluster = list(open_points)
    for p in sorted(new, key=lambda p: p.at):
        if cluster and p.at <= cluster[-1].at:
            continue  # a re-sent or out-of-order point
        if cluster:
            lat, lon = _centre(cluster)
            silent = p.at - cluster[-1].at > MAX_SILENCE
            if distance_m(lat, lon, p.lat, p.lon) <= STAY_RADIUS_M and not silent:
                cluster.append(p)
                continue
            if cluster[-1].at - cluster[0].at >= MIN_STAY:
                closed.append((cluster[0].at, cluster[-1].at, lat, lon))
        cluster = [p]
    return closed, cluster


def nearest_place(index: PlaceIndex, lat: float, lon: float) -> dict[str, Any] | None:
    """The closest known place with coordinates within PLACE_RADIUS_M, if any
    (towns and regions are too big to be "here")."""
    best: tuple[float, dict[str, Any]] | None = None
    for node in index.nodes:
        if node.get("kind") in ("country", "region", "town", "park"):
            continue
        n_lat, n_lon = _num(node.get("lat")), _num(node.get("lon"))
        if n_lat is None or n_lon is None:
            continue
        d = distance_m(lat, lon, n_lat, n_lon)
        if d <= PLACE_RADIUS_M and (best is None or d < best[0]):
            best = (d, node)
    return best[1] if best else None


def _stamp(moment: datetime, tz: tzinfo) -> str:
    local = moment.astimezone(tz)
    return local.strftime("%Y-%m-%d %H:%M:%S %z")


def visits_by_day(
    closed: list[tuple[datetime, datetime, float, float]], index: PlaceIndex, tz: tzinfo
) -> dict[date, list[dict[str, Any]]]:
    """Closed stays as visit records, split at local midnight, by day."""
    out: dict[date, list[dict[str, Any]]] = {}
    for start, end, lat, lon in closed:
        node = nearest_place(index, lat, lon)
        a, b = start.astimezone(tz), end.astimezone(tz)
        while a < b:
            midnight = datetime.combine(a.date() + timedelta(days=1), datetime.min.time(), a.tzinfo)
            piece_end = min(b, midnight - timedelta(seconds=1))
            record: dict[str, Any] = {
                "start": _stamp(a, tz),
                "end": _stamp(piece_end, tz),
                "lat": round(lat, 4),
                "lon": round(lon, 4),
            }
            if node is not None:
                record["place"] = str(node["id"])
                record["name"] = str(node.get("name") or node["id"])
            out.setdefault(a.date(), []).append(record)
            a = midnight
    return out


def day_file(day: date) -> str:
    return f"{DAYS_DIR}/{day.isoformat()}.yaml"


def merge_visits(existing: Any, new: list[dict[str, Any]]) -> dict[str, Any]:
    """A day file with visits added; one already there (same start) is replaced."""
    current = [dict(v) for v in (existing or {}).get("visits") or [] if isinstance(v, dict)]
    by_start = {v.get("start"): v for v in current}
    for v in new:
        by_start[v["start"]] = v
    ordered = sorted(by_start.values(), key=lambda v: health.wall(v.get("start")) or datetime.min)
    return {"visits": ordered}


def name_visits(vault: Vault, day: date, place_id: str, name: str, lat: float, lon: float) -> int:
    """Name the day's unnamed visits within PLACE_RADIUS_M of a place just
    located; returns how many."""
    data = vault.read_yaml(day_file(day), None)
    visits = [dict(v) for v in (data or {}).get("visits") or [] if isinstance(v, dict)]
    named = 0
    for v in visits:
        if v.get("place") or not isinstance(v.get("lat"), int | float):
            continue
        if distance_m(float(v["lat"]), float(v["lon"]), lat, lon) <= PLACE_RADIUS_M:
            v["place"], v["name"] = place_id, name
            named += 1
    if named:
        vault.write_yaml(
            day_file(day), {"visits": visits}, f"location: name {named} visit(s) {name}"
        )
    return named


def read_days(vault: Vault, start: date, end: date) -> dict[date, list[dict[str, Any]]]:
    out: dict[date, list[dict[str, Any]]] = {}
    day = start
    while day <= end:
        data = vault.read_yaml(day_file(day), None)
        visits = [dict(v) for v in (data or {}).get("visits") or [] if isinstance(v, dict)]
        if visits:
            out[day] = visits
        day += timedelta(days=1)
    return out


def _points_state(raw: Any) -> list[Point]:
    out = []
    for p in raw if isinstance(raw, list) else []:
        at = _when(p.get("at")) if isinstance(p, dict) else None
        if at and isinstance(p.get("lat"), int | float) and isinstance(p.get("lon"), int | float):
            out.append(Point(at, float(p["lat"]), float(p["lon"])))
    return out


def ingest(
    vault: Vault, body: bytes, index: PlaceIndex, tz: tzinfo
) -> tuple[str, Any, dict[date, list[dict[str, Any]]]]:
    """Store one post; returns (log line, the reply the sender expects, the
    visits it closed by day). Overland wants {"result": "ok"} or it retries;
    OwnTracks a JSON list."""
    if len(body) > MAX_BODY_BYTES:
        raise LocationError("body too large")
    try:
        data = json.loads(body)
    except (ValueError, UnicodeDecodeError) as exc:
        raise LocationError("not JSON") from exc
    reply: Any = {"result": "ok"} if isinstance(data, dict) and "locations" in data else []
    points = parse(data)
    if not points:
        return "no usable points", reply, {}
    with _LOCK:
        spool = vault.spool()
        open_points = _points_state(spool.load_state(STATE, []))
        closed, still_open = stays(open_points, points)
        spool.save_state(
            STATE,
            [{"at": p.at.isoformat(), "lat": p.lat, "lon": p.lon} for p in still_open],
        )
        files: dict[str, Any] = {}
        new_visits = visits_by_day(closed, index, tz)
        for day, visits in new_visits.items():
            files[day_file(day)] = merge_visits(vault.read_yaml(day_file(day), {}), visits)
        if files:
            vault.write_yaml_many(files, f"location: {len(closed)} visit(s)")
    return f"{len(points)} points, {len(closed)} visit(s) closed", reply, new_visits


def current(vault: Vault) -> tuple[float, float, datetime] | None:
    """Where the user is now (the stay in progress, or the last point), if known."""
    points = _points_state(vault.spool().load_state(STATE, []))
    if not points:
        return None
    lat, lon = _centre(points)
    return lat, lon, points[-1].at
