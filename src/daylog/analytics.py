"""Read-only website analytics from PostHog, for the personal site.

Two uses: a short "Site" block at the end of the morning brief (the last
24 hours, including anyone who came in through a company `?ref=` tag),
and `/site [days]` for a deeper breakdown on demand. Both are HogQL
queries against the PostHog query API (project POSTHOG_PROJECT_ID) with a
read-only personal key from POSTHOG_PERSONAL_API_KEY. Nothing polls.

Only summaries reach the vault (`site.yaml`), written once a day with the
brief: counts, refs, dates, a country. No IPs, person ids or recordings.
"""

from __future__ import annotations

import logging
import os
from dataclasses import asdict, dataclass
from datetime import UTC, date, datetime, timedelta
from typing import Any

import httpx

logger = logging.getLogger(__name__)

FILE = "site.yaml"
HOST = "https://us.posthog.com"
KEEP_DAYS = 120
MAX_REPORT_DAYS = 90

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


def format_visit(visit: Visit) -> str:
    did = []
    if visit.resume:
        did.append("resume")
    if visit.chess:
        did.append("chess")
    if visit.spacetime:
        did.append("spacetime")
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


def fetch_report_data(
    days: int, client: httpx.Client | None = None, today: date | None = None
) -> dict[str, Any]:
    """The /site numbers as data: totals, visitors by day, top lists, company refs.

    Counts are people, with `n` the page views (or events) behind them.
    """
    days = max(1, min(int(days), MAX_REPORT_DAYS))
    today = today or datetime.now(UTC).date()
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
        **{key: [] for key, *_ in _SECTIONS},
        "engagement": None,
        "company": [],
    }
    if not summary["people"] and not summary["resume"]:
        return data
    data["by_day"] = _by_day(query(breakdown_hogql(_DAY, days, days + 1), client), days, today)
    for key, _title, expr, limit in _SECTIONS:
        data[key] = _rows(query(breakdown_hogql(expr, days, limit), client))
    data["engagement"] = _engagement((query(engagement_hogql(days), client) or [{}])[0])
    data["company"] = [
        {**asdict(v), "recording": recording_url(v.session) if v.session else None}
        for v in visits(query(visits_hogql(days), client))
    ]
    return data


def format_report(data: dict[str, Any]) -> str:
    days = data["days"]
    if not data["people"] and not data["resume"]:
        return f"Site, last {days} days: no visitors."
    summary = {**data, "by_ref": {r["key"]: r["people"] for r in data["by_ref"]}}
    lines = [f"Site, last {days} days", *_headline(summary)]
    sections = [("By day", [r for r in data["by_day"] if r["people"] or r["n"]])]
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
    lines.append("\n(people, with page views or events in brackets)")
    return "\n".join(line for line in lines if line)


def fetch_report(days: int, client: httpx.Client | None = None) -> str:
    return format_report(fetch_report_data(days, client))
