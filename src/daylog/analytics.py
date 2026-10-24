"""Read-only website analytics from PostHog, for the personal site.

Two uses: a short "Site" block at the end of the morning brief (the last
24 hours, including anyone who came in through a company `?ref=` tag),
and `/site [days]` for a deeper breakdown on demand, down to each of the
latest MAX_SESSIONS visits (where from, on what, through which link, the
pages in order, what they tried, a replay when one exists). Both are HogQL
queries against the PostHog query API (project POSTHOG_PROJECT_ID) with a
read-only personal key from POSTHOG_PERSONAL_API_KEY. Nothing polls.

Only summaries reach the vault (`site.yaml`), written once a day with the
brief: counts, refs, dates, a country. No IPs, person ids or recordings.
"""

from __future__ import annotations

import logging
import os
import re
from collections import Counter
from dataclasses import asdict, dataclass
from datetime import UTC, date, datetime, timedelta, tzinfo
from typing import Any

import httpx

from daylog import runtime

logger = logging.getLogger(__name__)

FILE = "site.yaml"
HOST = "https://us.posthog.com"
KEEP_DAYS = 120
MAX_REPORT_DAYS = 90
MAX_SESSIONS = 50
MAX_SESSION_PAGES = 30

TEST_REFS = ("test", "claude-test")
# Refs that are channels, not companies.
CHANNEL_REFS = ("linkedin", "github")
CHESS_EVENTS = ("chess_engine_loaded", "chess_action", "chess_new_game")
SPACETIME_EVENTS = ("spacetime_source", "spacetime_export")

_REF = "lower(ifNull(toString(properties.ref), ''))"
_NOT_TEST = (
    f"distinct_id != 'claude-test' AND {_REF} NOT IN ({', '.join(repr(r) for r in TEST_REFS)})"
)


class AnalyticsError(RuntimeError):
    pass


def configured() -> bool:
    return bool(os.environ.get("POSTHOG_PERSONAL_API_KEY") and os.environ.get("POSTHOG_PROJECT_ID"))


def project_id() -> str:
    return os.environ.get("POSTHOG_PROJECT_ID", "")


def recording_url(session_id: str) -> str:
    return f"{HOST}/project/{project_id()}/replay/{session_id}"


def query(hogql: str, client: httpx.Client | None = None) -> list[dict[str, Any]]:
    """Run one HogQL query; rows come back as dicts keyed by column name."""
    key = os.environ.get("POSTHOG_PERSONAL_API_KEY")
    if not configured() or not key:
        raise AnalyticsError("POSTHOG_PERSONAL_API_KEY / POSTHOG_PROJECT_ID not set")
    http = client or httpx.Client(timeout=30.0)
    try:
        response = http.post(
            f"{HOST}/api/projects/{project_id()}/query/",
            headers={"Authorization": f"Bearer {key}"},
            json={"query": {"kind": "HogQLQuery", "query": hogql}},
        )
    except httpx.HTTPError as e:
        raise AnalyticsError(f"PostHog request failed: {type(e).__name__}") from e
    finally:
        if client is None:
            http.close()
    if response.status_code != 200:
        # The body names the problem (bad HogQL, bad scope); the key isn't in it.
        raise AnalyticsError(f"PostHog {response.status_code}: {response.text[:300]}")
    data = response.json()
    columns = data.get("columns") or []
    return [dict(zip(columns, row, strict=False)) for row in data.get("results") or []]


def _in(events: tuple[str, ...]) -> str:
    return "(" + ", ".join(repr(e) for e in events) + ")"


def _window(days: int) -> str:
    """Events in the last `days` days, minus setup tests. `days` is an int, never text."""
    return f"timestamp > now() - INTERVAL {int(days)} DAY AND {_NOT_TEST}"


# -- company-ref visitors -----------------------------------------------------


@dataclass
class Visit:
    ref: str
    first_seen: str
    pages: list[str]
    resume: int
    chess: int
    spacetime: int
    city: str
    country: str
    session: str


def visits_hogql(days: int) -> str:
    """One row per person+ref that arrived through a company ref, earliest first."""
    excluded = ", ".join(repr(r) for r in ("", *CHANNEL_REFS, *TEST_REFS))
    return (
        f"SELECT person_id, {_REF} AS ref, min(timestamp) AS first_seen, "
        "groupUniqArrayIf(properties.$pathname, event = '$pageview') AS pages, "
        "countIf(event = 'resume_download') AS resume, "
        f"countIf(event IN {_in(CHESS_EVENTS)}) AS chess, "
        f"countIf(event IN {_in(SPACETIME_EVENTS)}) AS spacetime, "
        "any(properties.$geoip_city_name) AS city, "
        "any(properties.$geoip_country_code) AS country, "
        "argMin(properties.$session_id, timestamp) AS session "
        f"FROM events WHERE {_window(days)} AND {_REF} NOT IN ({excluded}) "
        "GROUP BY person_id, ref ORDER BY first_seen LIMIT 100"
    )


def visits(rows: list[dict[str, Any]]) -> list[Visit]:
    return [
        Visit(
            ref=str(row["ref"]),
            first_seen=str(row.get("first_seen") or "")[:10],
            pages=sorted(str(p) for p in row.get("pages") or [] if p),
            resume=int(row.get("resume") or 0),
            chess=int(row.get("chess") or 0),
            spacetime=int(row.get("spacetime") or 0),
            city=str(row.get("city") or ""),
            country=str(row.get("country") or ""),
            session=str(row.get("session") or ""),
        )
        for row in rows
        if row.get("ref")
    ]


def _tried(resume: int, chess: int, spacetime: int) -> list[str]:
    return [k for k, n in (("resume", resume), ("chess", chess), ("spacetime", spacetime)) if n]


def format_visit(visit: Visit) -> str:
    did = _tried(visit.resume, visit.chess, visit.spacetime)
    where = ", ".join(x for x in (visit.city, visit.country) if x)
    bits = [
        f"{len(visit.pages)} page{'s' if len(visit.pages) != 1 else ''}"
        + (f" ({', '.join(visit.pages[:5])})" if visit.pages else ""),
        "tried " + ", ".join(did) if did else "",
        where,
    ]
    line = f"• {visit.ref}: " + " · ".join(b for b in bits if b)
    if visit.session:
        line += f"\n  {recording_url(visit.session)}"
    return line


# -- totals -------------------------------------------------------------------


def totals_hogql(days: int) -> str:
    return (
        "SELECT uniqIf(person_id, event = '$pageview') AS people, "
        "countIf(event = '$pageview') AS views, "
        "countIf(event = 'resume_download') AS resume, "
        f"uniqIf(person_id, event IN {_in(CHESS_EVENTS)}) AS chess, "
        f"uniqIf(person_id, event IN {_in(SPACETIME_EVENTS)}) AS spacetime "
        f"FROM events WHERE {_window(days)}"
    )


def breakdown_hogql(expr: str, days: int, limit: int = 10, event: str = "$pageview") -> str:
    """People and count per value of `expr` (a fixed expression from this module)."""
    return (
        f"SELECT {expr} AS key, uniq(person_id) AS people, count() AS n "
        f"FROM events WHERE event = '{event}' AND {_window(days)} "
        f"GROUP BY key ORDER BY people DESC, n DESC LIMIT {int(limit)}"
    )


def summarize(totals: list[dict[str, Any]], by_ref: list[dict[str, Any]]) -> dict[str, Any]:
    row = totals[0] if totals else {}
    refs = {str(r.get("key") or "") or "(none)": int(r.get("people") or 0) for r in by_ref}
    return {
        "people": int(row.get("people") or 0),
        "views": int(row.get("views") or 0),
        "resume": int(row.get("resume") or 0),
        "chess_people": int(row.get("chess") or 0),
        "spacetime_people": int(row.get("spacetime") or 0),
        "by_ref": refs,
        "company_refs": [r for r in refs if r not in ("(none)", *CHANNEL_REFS)],
    }


def _headline(summary: dict[str, Any]) -> list[str]:
    refs = " · ".join(f"{ref} {n}" for ref, n in summary["by_ref"].items())
    return [
        f"Visitors: {summary['people']} ({summary['views']} page views)",
        f"By ref: {refs}" if refs else "",
        f"Resume downloads: {summary['resume']} · chess: {summary['chess_people']} people"
        f" · spacetime: {summary['spacetime_people']} people",
    ]


# -- the brief's block --------------------------------------------------------


def fetch_brief(client: httpx.Client | None = None) -> tuple[dict[str, Any], list[Visit]]:
    summary = summarize(
        query(totals_hogql(1), client), query(breakdown_hogql(_REF, 1, limit=20), client)
    )
    company = visits(query(visits_hogql(1), client)) if summary["company_refs"] else []
    return summary, company


def format_brief(summary: dict[str, Any], company: list[Visit]) -> str:
    if not summary["people"] and not summary["resume"]:
        return "Site (last 24 h): no visitors."
    lines = ["Site (last 24 h)", *_headline(summary)]
    if company:
        lines.append("Company refs:")
        lines += [format_visit(v) for v in company]
    lines.append("/site for more")
    return "\n".join(line for line in lines if line)


def record_day(
    state: dict[str, Any], day: str, summary: dict[str, Any], company: list[Visit]
) -> dict[str, Any]:
    """Add one day's summary (no ids or cities), keeping the last KEEP_DAYS days."""
    days = dict(state.get("days") or {})
    days[day] = {
        **summary,
        "company_visits": [
            {
                "ref": v.ref,
                "pages": len(v.pages),
                "resume": v.resume > 0,
                "chess": v.chess > 0,
                "spacetime": v.spacetime > 0,
                "country": v.country,
            }
            for v in company
        ],
    }
    return {**state, "days": dict(sorted(days.items())[-KEEP_DAYS:])}


# -- /site: the deeper read ---------------------------------------------------

# (data key, title in the text report, HogQL expression, row limit)
_SECTIONS = (
    ("pages", "Top pages", "properties.$pathname", 10),
    (
        "sources",
        "Came from",
        "ifNull(nullIf(domain(toString(properties.$referrer)), ''), '(direct)')",
        8,
    ),
    ("countries", "Countries", "ifNull(toString(properties.$geoip_country_code), '?')", 8),
    ("devices", "Devices", "ifNull(toString(properties.$device_type), '?')", 5),
)
_DAY = "toString(toDate(timestamp))"
_HOUR = "toStartOfHour(timestamp)"
_SOURCE = "ifNull(nullIf(domain(toString(properties.$referrer)), ''), '(direct)')"
_SESSION_ID = re.compile(r"[0-9A-Za-z-]{1,64}")


# -- one row per visit --------------------------------------------------------


@dataclass
class Session:
    """One browser session: who (roughly), from where, and what they did."""

    session: str
    start: str
    seconds: int
    pages: list[str]
    ref: str
    source: str
    city: str
    region: str
    country: str
    device: str
    browser: str
    os: str
    resume: int
    chess: int
    games: int
    moves: int
    spacetime: int
    sources: int
    exports: int
    visits: int  # this person's sessions among those listed


def sessions_hogql(days: int, limit: int = MAX_SESSIONS) -> str:
    """The latest `limit` sessions in the window, most recent first.

    Page views come back as (time, path) pairs; they're put in order here,
    not in HogQL, where groupArray's order isn't guaranteed.
    """
    return (
        "SELECT toString(properties.$session_id) AS session, any(person_id) AS person, "
        "min(timestamp) AS start, dateDiff('second', min(timestamp), max(timestamp)) AS seconds, "
        "groupArrayIf(tuple(toString(timestamp), toString(properties.$pathname)), "
        "event = '$pageview') AS views, "
        f"max({_REF}) AS ref, argMin({_SOURCE}, timestamp) AS source, "
        "any(properties.$geoip_city_name) AS city, "
        "any(properties.$geoip_subdivision_1_name) AS region, "
        "any(properties.$geoip_country_code) AS country, "
        "any(properties.$device_type) AS device, any(properties.$browser) AS browser, "
        "any(properties.$os) AS os, "
        "countIf(event = 'resume_download') AS resume, "
        f"countIf(event IN {_in(CHESS_EVENTS)}) AS chess, "
        "countIf(event = 'chess_new_game') AS chess_games, "
        "countIf(event = 'chess_action') AS chess_moves, "
        f"countIf(event IN {_in(SPACETIME_EVENTS)}) AS spacetime, "
        "countIf(event = 'spacetime_source') AS spacetime_sources, "
        "countIf(event = 'spacetime_export') AS spacetime_exports "
        f"FROM events WHERE {_window(days)} AND properties.$session_id IS NOT NULL "
        f"GROUP BY session ORDER BY start DESC LIMIT {int(limit)}"
    )


def _pages(views: Any) -> list[str]:
    """Paths in the order seen, a reload (the same path twice running) once."""
    pairs = sorted((str(t), str(p)) for t, p in (views or []) if p)
    pages: list[str] = []
    for _t, path in pairs:
        if not pages or pages[-1] != path:
            pages.append(path)
    return pages[:MAX_SESSION_PAGES]


def sessions(rows: list[dict[str, Any]]) -> list[Session]:
    rows = [r for r in rows if r.get("session")]
    people = Counter(str(r.get("person") or r["session"]) for r in rows)

    def text(row: dict[str, Any], key: str) -> str:
        return str(row.get(key) or "")

    def n(row: dict[str, Any], key: str) -> int:
        return int(row.get(key) or 0)

    return [
        Session(
            session=text(r, "session"),
            start=text(r, "start"),
            seconds=n(r, "seconds"),
            pages=_pages(r.get("views")),
            ref=text(r, "ref"),
            source=text(r, "source") or "(direct)",
            city=text(r, "city"),
            region=text(r, "region"),
            country=text(r, "country"),
            device=text(r, "device"),
            browser=text(r, "browser"),
            os=text(r, "os"),
            resume=n(r, "resume"),
            chess=n(r, "chess"),
            games=n(r, "chess_games"),
            moves=n(r, "chess_moves"),
            spacetime=n(r, "spacetime"),
            sources=n(r, "spacetime_sources"),
            exports=n(r, "spacetime_exports"),
            visits=people[str(r.get("person") or r["session"])],
        )
        for r in rows
    ]


def recordings_hogql(session_ids: list[str]) -> str:
    """Which of these sessions have a replay (ids are checked before they get here)."""
    ids = ", ".join(repr(s) for s in session_ids)
    return (
        "SELECT session_id FROM session_replay_events "
        f"WHERE session_id IN ({ids}) GROUP BY session_id"
    )


def _recorded(session_ids: list[str], client: httpx.Client | None) -> set[str] | None:
    """The ids PostHog holds a replay for; None when it can't say (then link them all)."""
    ids = sorted({s for s in session_ids if _SESSION_ID.fullmatch(s)})
    if not ids:
        return set()
    try:
        return {str(r.get("session_id")) for r in query(recordings_hogql(ids), client)}
    except AnalyticsError as exc:
        logger.info("replay lookup failed, linking every session: %s", exc)
        return None


def _local(stamp: str, zone: tzinfo) -> datetime | None:
    try:
        moment = datetime.fromisoformat(stamp.replace(" ", "T"))
    except ValueError:
        return None
    return (moment if moment.tzinfo else moment.replace(tzinfo=UTC)).astimezone(zone)


def _duration(seconds: int) -> str:
    if seconds < 60:
        return f"{seconds} s"
    if seconds < 3600:
        return f"{seconds // 60} min"
    return f"{seconds // 3600} h {seconds % 3600 // 60} min"


def format_session(s: dict[str, Any], zone: tzinfo) -> str:
    when = _local(s["start"], zone)
    stamp = when.strftime("%b %d %H:%M") if when else s["start"][:16]
    where = ", ".join(x for x in (s["city"], s["country"]) if x) or "?"
    came = s["ref"] or s["source"]
    did = _tried(s["resume"], s["chess"], s["spacetime"])
    bits = [
        where,
        s["device"].lower(),
        came,
        f"{len(s['pages'])} page{'s' if len(s['pages']) != 1 else ''}",
        "tried " + ", ".join(did) if did else "",
        _duration(s["seconds"]) if s["seconds"] else "",
    ]
    return f"  {stamp} " + " · ".join(b for b in bits if b)


def engagement_hogql(days: int) -> str:
    return (
        "SELECT countIf(event = 'chess_new_game') AS games, "
        "countIf(event = 'chess_action') AS moves, "
        "avgIf(toFloat(properties.seconds), event = 'chess_engine_loaded') AS engine_s, "
        "countIf(event = 'spacetime_source') AS sources, "
        "countIf(event = 'spacetime_export') AS exports "
        f"FROM events WHERE {_window(days)}"
    )


def _rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        {
            "key": str(r.get("key") or "?"),
            "people": int(r.get("people") or 0),
            "n": int(r.get("n") or 0),
        }
        for r in rows
    ]


def _engagement(row: dict[str, Any]) -> dict[str, Any] | None:
    if not any(row.get(k) for k in ("games", "moves", "sources", "exports")):
        return None
    engine = row.get("engine_s")
    # avgIf is NaN when nothing loaded, and NaN != NaN.
    seconds = None
    if isinstance(engine, int | float) and engine == engine and engine:
        seconds = round(float(engine), 1)
    return {
        "games": int(row.get("games") or 0),
        "moves": int(row.get("moves") or 0),
        "engine_s": seconds,
        "sources": int(row.get("sources") or 0),
        "exports": int(row.get("exports") or 0),
    }


def _by_day(rows: list[dict[str, Any]], days: int, today: date) -> list[dict[str, Any]]:
    """Every UTC day of the window, oldest first, zero where nobody came.

    The window starts mid-day `days` days ago, so that partial first day is
    only kept when it has visitors.
    """
    seen = {r["key"]: r for r in _rows(rows)}
    keys = [(today - timedelta(days=i)).isoformat() for i in range(days, -1, -1)]
    if keys[0] not in seen:
        keys = keys[1:]
    return [seen.get(k) or {"key": k, "people": 0, "n": 0} for k in sorted({*keys, *seen})]


def _by_hour(rows: list[dict[str, Any]], now: datetime, zone: tzinfo) -> list[dict[str, Any]]:
    """The last 24 hours by local hour ("2026-10-24T09:00"), oldest first, zeros filled.

    Like `_by_day`, the window's partial first hour is kept only with visitors.
    """
    seen: dict[str, dict[str, Any]] = {}
    for r in _rows(rows):
        moment = _local(r["key"], zone)
        if moment is None:
            continue
        key = moment.strftime("%Y-%m-%dT%H:%M")
        prev = seen.get(key) or {"key": key, "people": 0, "n": 0}
        seen[key] = {"key": key, "people": prev["people"] + r["people"], "n": prev["n"] + r["n"]}
    end = now.astimezone(UTC).replace(minute=0, second=0, microsecond=0)
    keys = [
        (end - timedelta(hours=i)).astimezone(zone).strftime("%Y-%m-%dT%H:%M")
        for i in range(24, -1, -1)
    ]
    if keys[0] not in seen:
        keys = keys[1:]
    return [seen.get(k) or {"key": k, "people": 0, "n": 0} for k in sorted({*keys, *seen})]


def _soft(hogql: str, client: httpx.Client | None) -> list[dict[str, Any]]:
    """A query the report can do without (the hour chart, the visit list):
    a failure is logged and leaves that part empty rather than the report."""
    try:
        return query(hogql, client)
    except AnalyticsError as exc:
        logger.warning("site report: optional query failed: %s", exc)
        return []


def fetch_report_data(
    days: int,
    client: httpx.Client | None = None,
    today: date | None = None,
    now: datetime | None = None,
    zone: tzinfo | None = None,
) -> dict[str, Any]:
    """The /site numbers as data: totals, a chart, top lists, each recent visit.

    Counts are people, with `n` the page views (or events) behind them. The
    chart is visitors by local hour for one day (`by_hour`), else by UTC day
    (`by_day`); the other one stays empty.
    """
    days = max(1, min(int(days), MAX_REPORT_DAYS))
    now = now or datetime.now(UTC)
    today = today or now.astimezone(UTC).date()
    zone = zone or runtime.tz()
    summary = summarize(
        query(totals_hogql(days), client), query(breakdown_hogql(_REF, days, limit=20), client)
    )
    data: dict[str, Any] = {
        "days": days,
        **{k: summary[k] for k in ("people", "views", "resume")},
        "chess_people": summary["chess_people"],
        "spacetime_people": summary["spacetime_people"],
        "by_ref": [{"key": k, "people": n} for k, n in summary["by_ref"].items()],
        "by_day": [],
        "by_hour": [],
        **{key: [] for key, *_ in _SECTIONS},
        "engagement": None,
        "visitors": [],
        "company": [],
    }
    if not summary["people"] and not summary["resume"]:
        return data
    if days == 1:
        data["by_hour"] = _by_hour(_soft(breakdown_hogql(_HOUR, 1, 26), client), now, zone)
    else:
        data["by_day"] = _by_day(query(breakdown_hogql(_DAY, days, days + 1), client), days, today)
    for key, _title, expr, limit in _SECTIONS:
        data[key] = _rows(query(breakdown_hogql(expr, days, limit), client))
    data["engagement"] = _engagement((query(engagement_hogql(days), client) or [{}])[0])
    listed = sessions(_soft(sessions_hogql(days), client))
    company = visits(query(visits_hogql(days), client))
    recorded = _recorded([s.session for s in listed] + [v.session for v in company], client)

    def replay(session_id: str) -> str | None:
        if not session_id or (recorded is not None and session_id not in recorded):
            return None
        return recording_url(session_id)

    data["visitors"] = [{**asdict(s), "recording": replay(s.session)} for s in listed]
    data["company"] = [{**asdict(v), "recording": replay(v.session)} for v in company]
    return data


def format_report(data: dict[str, Any], zone: tzinfo | None = None) -> str:
    days = data["days"]
    span = "24 h" if days == 1 else f"{days} days"
    if not data["people"] and not data["resume"]:
        return f"Site, last {span}: no visitors."
    zone = zone or runtime.tz()
    summary = {**data, "by_ref": {r["key"]: r["people"] for r in data["by_ref"]}}
    lines = [f"Site, last {span}", *_headline(summary)]
    sections = [
        ("By day", [r for r in data["by_day"] if r["people"] or r["n"]]),
        ("By hour", [r for r in data.get("by_hour") or [] if r["people"] or r["n"]]),
    ]
    sections += [(title, data[key]) for key, title, *_ in _SECTIONS]
    for title, rows in sections:
        if rows:
            lines.append(f"\n{title}")
            lines += [f"  {r['key']}: {r['people']} ({r['n']})" for r in rows]
    eng = data["engagement"]
    if eng:
        engine = eng["engine_s"]
        lines.append("\nEngagement")
        lines.append(
            f"  chess: {eng['games']} games, {eng['moves']} moves"
            + (f", engine loads in {engine:.1f} s" if engine else "")
        )
        lines.append(f"  spacetime: {eng['sources']} sources, {eng['exports']} exports")
    company = [Visit(**{k: v for k, v in c.items() if k != "recording"}) for c in data["company"]]
    if company:
        lines.append("\nCompany refs")
        lines += [f"{v.first_seen} {format_visit(v)[2:]}" for v in company]
    listed = data.get("visitors") or []
    if listed:
        lines.append(f"\nLatest visits ({len(listed)})")
        lines += [format_session(s, zone) for s in listed[:20]]
    lines.append("\n(people, with page views or events in brackets)")
    return "\n".join(line for line in lines if line)


def fetch_report(days: int, client: httpx.Client | None = None) -> str:
    return format_report(fetch_report_data(days, client))
