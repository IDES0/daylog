#!/usr/bin/env python3
"""daylog screen-time forwarder: ActivityWatch on this Mac -> daylog's health inlet.

Reads the local ActivityWatch server (http://localhost:5600/api/0/), works out
which app (and, in a browser, which website) was in front while the user was at
the keyboard, and POSTs those sessions to daylog:

    POST <DAYLOG_INLET_URL>   {"screen": [{"device": "mac", "app": "Dia",
        "site": "youtube.com", "start": "2026-11-20 22:10:05 -0600",
        "end": "2026-11-20 23:40:12 -0600"}]}

Only the app name and the bare domain leave the machine: never a window title
and never a full URL. Stdlib only, Python 3.9+, so macOS's system python runs it.

How a session is made:
1. window-watcher events (which app is in front) are cut to the not-afk periods
   of the AFK watcher, so idle and locked time drops out;
2. browser time is split again by the web watcher's events, each piece tagged
   with its site's domain (the app stays the browser);
3. pieces with the same app+site less than 30 s apart are joined, fragments
   shorter than MIN_SECONDS dropped, and the survivors joined once more.

Each run sends from (synced_until - 2 h) to now. The overlap re-sends sessions
that grew; daylog keys a session on device/app/site/start, so a re-send replaces
it. synced_until (state.json) moves only after every batch got a 2xx.

Config: env vars, else ~/Library/Application Support/daylog-screentime/config.json
(same keys): DAYLOG_INLET_URL (required unless --dry-run), AW_URL
(http://localhost:5600), LOOKBACK_HOURS (26, first run), MIN_SECONDS (20).
"""

from __future__ import annotations

import argparse
import bisect
import json
import os
import re
import sys
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Iterable, Iterator, Mapping
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, NamedTuple

UTC = timezone.utc  # noqa: UP017 -- datetime.UTC needs 3.11; this runs on 3.9
SUPPORT_DIR = Path.home() / "Library" / "Application Support" / "daylog-screentime"
CONFIG_FILE = SUPPORT_DIR / "config.json"
STATE_FILE = SUPPORT_DIR / "state.json"
USER_AGENT = "daylog-screentime/1"

DEVICE = "mac"
DEFAULT_AW_URL = "http://localhost:5600"
DEFAULT_LOOKBACK_HOURS = 26.0
DEFAULT_MIN_SECONDS = 20.0
OVERLAP = timedelta(hours=2)  # re-sent each run so grown sessions get their new end
CONTEXT = timedelta(hours=12)  # fetched before the send window so starts stay stable
SETTLE = timedelta(minutes=5)  # sessions this new wait a run: AFK is decided ~3 min late
MAX_BACKLOG = timedelta(days=14)
GAP = timedelta(seconds=30)
MAX_PIECE = timedelta(hours=12)  # the server refuses sessions over 16 h
BATCH_SIZE = 2000

# Browser app name (as macOS reports it) -> which web-watcher buckets describe it.
BROWSERS = {
    "Dia": "chromium",
    "Arc": "chromium",
    "Google Chrome": "chromium",
    "Brave Browser": "chromium",
    "Microsoft Edge": "chromium",
    "Safari": "safari",
    "Firefox": "firefox",
    "Orion": "any",  # WebKit, but runs both Chrome and Firefox extensions
}
IGNORED_APPS = {"loginwindow", "ScreenSaverEngine"}

_STAMP = re.compile(
    r"^(\d{4}-\d{2}-\d{2})[T ](\d{2}:\d{2}:\d{2})(?:\.(\d+))?\s*(Z|[+-]\d{2}:?\d{2})?$"
)


class Piece(NamedTuple):
    app: str
    site: str
    start: datetime
    end: datetime

    @property
    def seconds(self) -> float:
        return (self.end - self.start).total_seconds()


Interval = tuple[datetime, datetime]


def log(message: str) -> None:
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    print(f"{now} daylog-screentime: {message}", file=sys.stderr, flush=True)


# -- pure logic ---------------------------------------------------------------------


def parse_timestamp(value: str) -> datetime:
    """An ActivityWatch ISO timestamp (any fraction length, Z or offset) as aware UTC."""
    m = _STAMP.match(value.strip())
    if not m:
        raise ValueError(f"not a timestamp: {value!r}")
    day, clock, fraction, zone = m.groups()
    micro = (fraction or "0")[:6].ljust(6, "0")
    if not zone or zone == "Z":
        zone = "+00:00"
    elif ":" not in zone:
        zone = f"{zone[:3]}:{zone[3:]}"
    return datetime.fromisoformat(f"{day}T{clock}.{micro}{zone}").astimezone(UTC)


def event_interval(event: Mapping[str, Any]) -> Interval | None:
    """An event's (start, end), or None if it is malformed or empty."""
    try:
        start = parse_timestamp(str(event["timestamp"]))
        seconds = float(event.get("duration") or 0)
    except (KeyError, TypeError, ValueError):
        return None
    if seconds <= 0:
        return None
    return start, start + timedelta(seconds=seconds)


def merge_intervals(intervals: Iterable[Interval]) -> list[Interval]:
    """Sorted, with overlapping or touching intervals joined."""
    out: list[Interval] = []
    for start, end in sorted(intervals):
        if out and start <= out[-1][1]:
            if end > out[-1][1]:
                out[-1] = (out[-1][0], end)
        else:
            out.append((start, end))
    return out


def active_intervals(afk_events: Iterable[Mapping[str, Any]]) -> list[Interval]:
    """The not-afk periods, merged."""
    spans = []
    for event in afk_events:
        data = event.get("data") or {}
        if data.get("status") == "not-afk":
            span = event_interval(event)
            if span:
                spans.append(span)
    return merge_intervals(spans)


def _clip(start: datetime, end: datetime, lo: datetime, hi: datetime) -> Interval | None:
    start, end = max(start, lo), min(end, hi)
    return (start, end) if end > start else None


def _untangle(pieces: Iterable[Piece]) -> list[Piece]:
    """Sorted and made non-overlapping: a piece starting inside an earlier one is trimmed."""
    out: list[Piece] = []
    reach: datetime | None = None
    for p in sorted(pieces, key=lambda p: (p.start, p.end)):
        if reach is not None and p.start < reach:
            p = p._replace(start=reach)
        if p.end > p.start:
            out.append(p)
            reach = p.end if reach is None else max(reach, p.end)
    return out


def window_pieces(
    window_events: Iterable[Mapping[str, Any]], lo: datetime, hi: datetime
) -> list[Piece]:
    """Window-watcher events as (app, start, end), within [lo, hi], non-overlapping.

    Only the app name is kept: the window title is dropped here.
    """
    pieces = []
    for event in window_events:
        app = str((event.get("data") or {}).get("app") or "").strip()
        span = event_interval(event)
        if not app or app in IGNORED_APPS or not span:
            continue
        clipped = _clip(span[0], span[1], lo, hi)
        if clipped:
            pieces.append(Piece(app, "", clipped[0], clipped[1]))
    return _untangle(pieces)


def intersect(pieces: Iterable[Piece], active: list[Interval]) -> list[Piece]:
    """The parts of each piece inside the (merged, sorted) active intervals."""
    starts = [a for a, _ in active]
    out = []
    for p in pieces:
        i = max(bisect.bisect_right(starts, p.start) - 1, 0)
        while i < len(active) and active[i][0] < p.end:
            a, b = max(p.start, active[i][0]), min(p.end, active[i][1])
            if b > a:
                out.append(p._replace(start=a, end=b))
            i += 1
    return out


def domain_of(url: str) -> str:
    """The bare lowercase domain of an http(s) URL ("www." stripped), else ""."""
    try:
        parts = urllib.parse.urlsplit(url.strip())
        host = parts.hostname or ""
    except ValueError:
        return ""
    if parts.scheme not in ("http", "https"):
        return ""
    host = host.lower().rstrip(".")
    if host.startswith("www."):
        host = host[4:]
    return host if re.fullmatch(r"[a-z0-9.-]{1,120}", host) else ""


def browser_family(app: str) -> str | None:
    """Which web-watcher family describes this app, or None if it is not a browser."""
    if app in BROWSERS:
        return BROWSERS[app]
    for name, family in BROWSERS.items():  # "Google Chrome Canary", "Safari Technology Preview"
        if app.startswith(name + " "):
            return family
    return None


def web_bucket_family(bucket_id: str) -> str:
    """aw-watcher-web-safari -> safari, -firefox -> firefox, anything else -> chromium."""
    name = bucket_id.lower()
    if "safari" in name:
        return "safari"
    if "firefox" in name:
        return "firefox"
    return "chromium"


def web_pieces(web_events: Iterable[Mapping[str, Any]]) -> list[Piece]:
    """Web-watcher events as (site, start, end), non-overlapping. URL and title dropped.

    A private (incognito) tab counts as browser time with no site.
    """
    pieces = []
    for event in web_events:
        data = event.get("data") or {}
        site = "" if data.get("incognito") else domain_of(str(data.get("url") or ""))
        span = event_interval(event)
        if span:
            pieces.append(Piece("", site, span[0], span[1]))
    return _untangle(pieces)


def split_by_web(pieces: Iterable[Piece], web: Mapping[str, list[Piece]]) -> list[Piece]:
    """Browser pieces cut by the web watcher's tabs, each part tagged with its site.

    `web` maps a family (see BROWSERS) to that family's untangled web pieces; "any"
    is consulted for browsers that can run either extension. Time in a browser that
    no tab covers stays with site "".
    """
    starts = {family: [w.start for w in items] for family, items in web.items()}
    out: list[Piece] = []
    for p in pieces:
        family = browser_family(p.app) or ""
        tabs = web.get(family)
        if not tabs:
            out.append(p)
            continue
        i = max(bisect.bisect_right(starts[family], p.start) - 1, 0)
        cursor = p.start
        while i < len(tabs) and tabs[i].start < p.end:
            a, b = max(tabs[i].start, cursor), min(tabs[i].end, p.end)
            if b > a:
                if a > cursor:
                    out.append(p._replace(start=cursor, end=a))
                out.append(p._replace(site=tabs[i].site, start=a, end=b))
                cursor = b
            i += 1
        if cursor < p.end:
            out.append(p._replace(start=cursor))
    return out


def coalesce(pieces: Iterable[Piece], gap: timedelta = GAP) -> list[Piece]:
    """Consecutive pieces with the same app and site, less than `gap` apart, joined."""
    out: list[Piece] = []
    for p in sorted(pieces, key=lambda p: (p.start, p.end)):
        last = out[-1] if out else None
        if last and (last.app, last.site) == (p.app, p.site) and p.start - last.end < gap:
            out[-1] = last._replace(end=max(last.end, p.end))
        else:
            out.append(p)
    return out


def drop_short(pieces: Iterable[Piece], min_seconds: float) -> list[Piece]:
    return [p for p in pieces if p.seconds >= min_seconds]


def split_long(pieces: Iterable[Piece], limit: timedelta = MAX_PIECE) -> list[Piece]:
    """Pieces longer than `limit` cut into consecutive chunks of at most `limit`."""
    out = []
    for p in pieces:
        start = p.start
        while p.end - start > limit:
            out.append(p._replace(start=start, end=start + limit))
            start += limit
        out.append(p._replace(start=start))
    return out


def compute_sessions(
    window_events: Iterable[Mapping[str, Any]],
    afk_events: Iterable[Mapping[str, Any]] | None,
    web_events: Mapping[str, Iterable[Mapping[str, Any]]],
    lo: datetime,
    hi: datetime,
    min_seconds: float = DEFAULT_MIN_SECONDS,
) -> list[Piece]:
    """Sessions in [lo, hi] from raw ActivityWatch events.

    `afk_events` None means there is no AFK watcher: window time is taken as is.
    `web_events` maps a family ("chromium", "safari", "firefox") to its events.
    """
    pieces = window_pieces(window_events, lo, hi)
    if afk_events is not None:
        pieces = intersect(pieces, active_intervals(afk_events))
    web = {family: web_pieces(events) for family, events in web_events.items()}
    web["any"] = _untangle(p for items in web.values() for p in items)
    pieces = split_by_web(pieces, web)
    pieces = coalesce(drop_short(coalesce(pieces), min_seconds))
    return split_long(pieces)


def select_for_sending(
    sessions: Iterable[Piece], send_from: datetime, fetched_from: datetime, now: datetime
) -> list[Piece]:
    """The sessions to send this run.

    Ending after `send_from`; not touching the start of the fetched range (their
    true start is unknown; an earlier run sent them); not begun in the last few
    minutes (the AFK watcher may still take that time back).
    """
    floor, ceiling = fetched_from + GAP, now - SETTLE
    return [s for s in sessions if s.end > send_from and floor <= s.start <= ceiling]


def wall_clock(moment: datetime, tz: Any = None) -> str:
    """Local wall clock with the offset in force at that instant: "2026-11-20 22:10:05 -0600"."""
    return moment.astimezone(tz).strftime("%Y-%m-%d %H:%M:%S %z")


def to_records(sessions: Iterable[Piece], tz: Any = None) -> list[dict[str, str]]:
    """The inlet's screen records: device, app, optional site, start, end."""
    out = []
    for s in sessions:
        if int(s.end.timestamp()) <= int(s.start.timestamp()):
            continue  # under a second once the fractions are dropped
        start, end = wall_clock(s.start, tz), wall_clock(s.end, tz)
        record = {"device": DEVICE, "app": s.app}
        if s.site:
            record["site"] = s.site
        record["start"], record["end"] = start, end
        out.append(record)
    return out


def batches(items: list[Any], size: int = BATCH_SIZE) -> Iterator[list[Any]]:
    for i in range(0, len(items), size):
        yield items[i : i + size]


def pick_buckets(
    buckets: Mapping[str, Mapping[str, Any]],
) -> tuple[str, str | None, dict[str, list[str]]]:
    """(window bucket, AFK bucket or None, {web family: [bucket ids]}). Raises LookupError.

    With several hosts' buckets (aw-sync, a renamed Mac), the window bucket updated
    most recently wins and the AFK bucket of the same host is preferred.
    """

    def kind(bucket_id: str, meta: Mapping[str, Any]) -> str:
        if bucket_id.startswith("aw-watcher-window") or meta.get("type") == "currentwindow":
            return "window"
        if bucket_id.startswith("aw-watcher-afk") or meta.get("type") == "afkstatus":
            return "afk"
        if bucket_id.startswith("aw-watcher-web") or meta.get("type") == "web.tab.current":
            return "web"
        return ""

    def recency(bucket_id: str) -> str:
        meta = buckets[bucket_id]
        return str(meta.get("last_updated") or meta.get("created") or "")

    found: dict[str, list[str]] = {"window": [], "afk": [], "web": []}
    for bucket_id, meta in buckets.items():
        k = kind(bucket_id, meta)
        if k:
            found[k].append(bucket_id)
    if not found["window"]:
        raise LookupError("no aw-watcher-window bucket: is ActivityWatch's window watcher on?")
    window = max(found["window"], key=recency)
    host = buckets[window].get("hostname")
    afks = [b for b in found["afk"] if buckets[b].get("hostname") == host] or found["afk"]
    afk = max(afks, key=recency) if afks else None
    local = {host, "unknown", "", None}
    webs = [b for b in found["web"] if buckets[b].get("hostname") in local] or found["web"]
    families: dict[str, list[str]] = {}
    for b in sorted(webs):
        families.setdefault(web_bucket_family(b), []).append(b)
    return window, afk, families


def host_of(url: str) -> str:
    """Just the host, for logs: the inlet URL carries a secret and is never printed."""
    try:
        return urllib.parse.urlsplit(url).hostname or "?"
    except ValueError:
        return "?"


# -- config, state and the network --------------------------------------------------


def load_config(env: Mapping[str, str], path: Path = CONFIG_FILE) -> dict[str, Any]:
    """Env vars over config.json over defaults. Raises ValueError on bad values."""
    stored: dict[str, Any] = {}
    if path.exists():
        stored = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(stored, dict):
            raise ValueError(f"{path} is not a JSON object")

    def get(key: str, default: Any) -> Any:
        value = env.get(key)
        if value in (None, ""):
            value = stored.get(key)
        return default if value in (None, "") else value

    url = str(get("DAYLOG_INLET_URL", "")).strip()
    if url:
        parts = urllib.parse.urlsplit(url)
        if parts.scheme not in ("http", "https") or not parts.hostname:
            raise ValueError("DAYLOG_INLET_URL must be a full http(s) URL")
    return {
        "inlet_url": url,
        "aw_url": str(get("AW_URL", DEFAULT_AW_URL)).rstrip("/"),
        "lookback_hours": float(get("LOOKBACK_HOURS", DEFAULT_LOOKBACK_HOURS)),
        "min_seconds": float(get("MIN_SECONDS", DEFAULT_MIN_SECONDS)),
    }


def load_state(path: Path = STATE_FILE) -> datetime | None:
    """synced_until, or None before the first successful run."""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return parse_timestamp(str(data["synced_until"]))
    except (OSError, ValueError, KeyError, TypeError):
        return None


def save_state(synced_until: datetime, path: Path = STATE_FILE) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps({"synced_until": synced_until.isoformat()}) + "\n", "utf-8")
    os.replace(tmp, path)


def _iso(moment: datetime) -> str:
    return moment.astimezone(UTC).isoformat()


def aw_get(base: str, path: str, params: Mapping[str, str] | None = None) -> Any:
    url = f"{base}/api/0/{path}"
    if params:
        url += "?" + urllib.parse.urlencode(params)
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(request, timeout=30) as response:
        return json.load(response)


def fetch_events(base: str, bucket: str, start: datetime, end: datetime) -> list[Any]:
    path = f"buckets/{urllib.parse.quote(bucket, safe='')}/events"
    events = aw_get(base, path, {"start": _iso(start), "end": _iso(end), "limit": "-1"})
    return events if isinstance(events, list) else []


def post_batch(url: str, records: list[dict[str, str]]) -> str:
    """POST one batch; the server's reply text. Raises urllib.error.URLError / OSError."""
    body = json.dumps({"screen": records}, separators=(",", ":")).encode("utf-8")
    request = urllib.request.Request(
        url,
        data=body,
        method="POST",
        headers={"Content-Type": "application/json", "User-Agent": USER_AGENT},
    )
    with urllib.request.urlopen(request, timeout=60) as response:
        reply: bytes = response.read(300)
    return reply.decode("utf-8", "replace").strip()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Send Mac screen time from ActivityWatch to daylog."
    )
    parser.add_argument("--dry-run", action="store_true", help="print the JSON, send nothing")
    parser.add_argument("--since", type=float, metavar="HOURS", help="send the last HOURS hours")
    args = parser.parse_args(argv)

    try:
        config = load_config(os.environ, CONFIG_FILE)
    except (OSError, ValueError) as exc:
        log(f"bad config: {exc}")
        return 2
    inlet = config["inlet_url"]
    if not inlet and not args.dry_run:
        log(f"no DAYLOG_INLET_URL (env or {CONFIG_FILE}); run install.sh")
        return 2

    now = datetime.now(UTC)
    synced_until = load_state(STATE_FILE)
    if args.since is not None:
        send_from = now - timedelta(hours=args.since)
    elif synced_until is not None:
        send_from = synced_until - OVERLAP
    else:
        send_from = now - timedelta(hours=config["lookback_hours"])
    send_from = min(max(send_from, now - MAX_BACKLOG), now)
    fetch_from = send_from - CONTEXT

    aw = config["aw_url"]
    try:
        window, afk, web_ids = pick_buckets(aw_get(aw, "buckets/"))
        window_events = fetch_events(aw, window, fetch_from, now)
        afk_events = fetch_events(aw, afk, fetch_from, now) if afk else None
        web_events: dict[str, list[Any]] = {}
        for family, ids in web_ids.items():
            for bucket in ids:
                web_events.setdefault(family, []).extend(fetch_events(aw, bucket, fetch_from, now))
    except LookupError as exc:
        log(str(exc))
        return 1
    except (OSError, ValueError) as exc:  # URLError is an OSError
        log(f"ActivityWatch not reachable at {aw}: {type(exc).__name__}: {exc}")
        return 1
    if afk is None:
        log("no AFK watcher bucket: idle time cannot be excluded")

    sessions = compute_sessions(
        window_events, afk_events, web_events, fetch_from, now, config["min_seconds"]
    )
    records = to_records(select_for_sending(sessions, send_from, fetch_from, now))
    sites = sum(1 for r in records if "site" in r)
    span = f"since {wall_clock(send_from)}"

    if args.dry_run:
        for batch in batches(records):
            print(json.dumps({"screen": batch}, indent=2))
        log(f"dry run: {len(records)} sessions ({sites} with a site) {span}; nothing sent")
        return 0

    target = host_of(inlet)
    for n, batch in enumerate(batches(records), 1):
        try:
            reply = post_batch(inlet, batch)
        except urllib.error.HTTPError as exc:
            try:
                detail = exc.read(200).decode("utf-8", "replace").strip()
            except OSError:
                detail = ""
            log(f"{target} refused batch {n}: HTTP {exc.code} {detail}")
            return 1
        except urllib.error.URLError as exc:  # its reason never holds the URL
            log(f"could not reach {target}: {exc.reason}")
            return 1
        except (OSError, ValueError) as exc:  # their text might quote the URL: type only
            log(f"could not reach {target}: {type(exc).__name__}")
            return 1
        if reply:
            log(f"batch {n} ({len(batch)}): {reply[:200]}")
    save_state(now, STATE_FILE)
    log(f"sent {len(records)} sessions ({sites} with a site) {span} to {target}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
