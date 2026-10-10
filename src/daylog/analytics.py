"""Read-only website analytics from PostHog, for the personal site.

Two uses: a Telegram alert when someone arrives through a company ref (a
`?ref=` tag handed out with a job application), and a "Site" section at
the end of the morning brief. Both are HogQL queries against the PostHog
query API (project POSTHOG_PROJECT_ID) with a read-only personal key from
POSTHOG_PERSONAL_API_KEY.

Only summaries reach the vault (`site.yaml`): counts, refs, dates, a
country, and hashes of person+ref so an alert never repeats. No IPs, no
person ids, no URLs.
"""

from __future__ import annotations

import hashlib
import logging
import os
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx

logger = logging.getLogger(__name__)

FILE = "site.yaml"
HOST = "https://us.posthog.com"
POLL_SECONDS = 15 * 60
# Events land in PostHog a little late; re-reading a short overlap is
# harmless because alerts are deduplicated by person+ref.
OVERLAP = timedelta(minutes=10)
FIRST_LOOKBACK = timedelta(days=1)
MAX_ALERTED = 1000
KEEP_DAYS = 120

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


def _ts(when: datetime) -> str:
    """A UTC timestamp literal; built from a datetime, never from query input."""
    return f"toDateTime('{when.astimezone(UTC).strftime('%Y-%m-%d %H:%M:%S')}', 'UTC')"


def _parse_ts(value: Any) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


# -- company-ref alerts -------------------------------------------------------


@dataclass
class Visit:
    person: str
    ref: str
    first_seen: datetime
    last_seen: datetime
    pages: list[str]
    resume: int
    chess: int
    spacetime: int
    city: str
    country: str
    session: str

    @property
    def key(self) -> str:
        """Hash of person+ref: enough to dedupe, nothing to identify anyone by."""
        return hashlib.sha256(f"{self.person}|{self.ref}".encode()).hexdigest()[:16]


def visits_hogql(since: datetime) -> str:
    """Company-ref visitors with any event after `since`, totalled over two days."""
    excluded = ", ".join(repr(r) for r in ("", *CHANNEL_REFS, *TEST_REFS))
    return (
        "SELECT toString(person_id) AS person, "
        f"{_REF} AS ref, "
        "min(timestamp) AS first_seen, max(timestamp) AS last_seen, "
        "groupUniqArrayIf(properties.$pathname, event = '$pageview') AS pages, "
        "countIf(event = 'resume_download') AS resume, "
        f"countIf(event IN {_in(CHESS_EVENTS)}) AS chess, "
        f"countIf(event IN {_in(SPACETIME_EVENTS)}) AS spacetime, "
        "any(properties.$geoip_city_name) AS city, "
        "any(properties.$geoip_country_code) AS country, "
        "argMin(properties.$session_id, timestamp) AS session "
        "FROM events "
        f"WHERE timestamp > now() - INTERVAL 2 DAY AND distinct_id != 'claude-test' "
        f"AND {_REF} NOT IN ({excluded}) "
        "GROUP BY person, ref "
        f"HAVING last_seen > {_ts(since)} "
        "ORDER BY first_seen LIMIT 200"
    )


def _visit(row: dict[str, Any]) -> Visit | None:
    first, last = _parse_ts(row.get("first_seen")), _parse_ts(row.get("last_seen"))
    if not row.get("person") or not row.get("ref") or first is None or last is None:
        return None
    return Visit(
        person=str(row["person"]),
        ref=str(row["ref"]),
        first_seen=first,
        last_seen=last,
        pages=sorted(str(p) for p in row.get("pages") or [] if p),
        resume=int(row.get("resume") or 0),
        chess=int(row.get("chess") or 0),
        spacetime=int(row.get("spacetime") or 0),
        city=str(row.get("city") or ""),
        country=str(row.get("country") or ""),
        session=str(row.get("session") or ""),
    )


def new_visits(
    state: dict[str, Any], rows: list[dict[str, Any]], now: datetime
) -> tuple[list[Visit], dict[str, Any]]:
    """Visits not alerted before, and the state to save once they're sent."""
    alerted = list(state.get("alerted") or [])
    seen = set(alerted)
    fresh: list[Visit] = []
    cursor = _parse_ts(state.get("cursor")) or now - FIRST_LOOKBACK
    for row in rows:
        visit = _visit(row)
        if visit is None:
            continue
        cursor = max(cursor, visit.last_seen)
        if visit.key in seen:
            continue
        seen.add(visit.key)
        alerted.append(visit.key)
        fresh.append(visit)
    log = list(state.get("company_visits") or [])
    log += [
        {
            "date": v.first_seen.date().isoformat(),
            "ref": v.ref,
            "pages": len(v.pages),
            "resume": v.resume > 0,
            "chess": v.chess > 0,
            "spacetime": v.spacetime > 0,
            "country": v.country,
        }
        for v in fresh
    ]
    new_state = dict(state)
    new_state.update(cursor=cursor.isoformat(), alerted=alerted[-MAX_ALERTED:], company_visits=log)
    return fresh, new_state


def poll_since(state: dict[str, Any], now: datetime) -> datetime:
    cursor = _parse_ts(state.get("cursor"))
    return (cursor - OVERLAP) if cursor else now - FIRST_LOOKBACK


def format_alert(visit: Visit) -> str:
    did = []
    if visit.resume:
        did.append("downloaded the resume")
    if visit.chess:
        did.append("played chess")
    if visit.spacetime:
        did.append("tried spacetime")
    where = ", ".join(x for x in (visit.city, visit.country) if x) or "unknown"
    lines = [
        f"🔔 Site visit from ref={visit.ref}",
        f"Pages: {', '.join(visit.pages) or '(none yet)'}",
        f"Did: {'; '.join(did) or 'just looked'}",
        f"From: {where}",
    ]
    if visit.session:
        lines.append(f"Recording: {recording_url(visit.session)}")
    return "\n".join(lines)


# -- morning brief ------------------------------------------------------------


def summary_hogql() -> tuple[str, str]:
    """(totals, by-ref) queries for the last 24 hours."""
    window = f"timestamp > now() - INTERVAL 1 DAY AND {_NOT_TEST}"
    totals = (
        "SELECT uniqIf(person_id, event = '$pageview') AS people, "
        "countIf(event = '$pageview') AS views, "
        "countIf(event = 'resume_download') AS resume, "
        f"uniqIf(person_id, event IN {_in(CHESS_EVENTS)}) AS chess, "
        f"uniqIf(person_id, event IN {_in(SPACETIME_EVENTS)}) AS spacetime "
        f"FROM events WHERE {window}"
    )
    by_ref = (
        f"SELECT {_REF} AS ref, uniq(person_id) AS people, count() AS views "
        f"FROM events WHERE event = '$pageview' AND {window} "
        "GROUP BY ref ORDER BY people DESC, views DESC LIMIT 50"
    )
    return totals, by_ref


def summarize(totals: list[dict[str, Any]], by_ref: list[dict[str, Any]]) -> dict[str, Any]:
    row = totals[0] if totals else {}
    refs = {str(r.get("ref") or "") or "(none)": int(r.get("people") or 0) for r in by_ref}
    return {
        "people": int(row.get("people") or 0),
        "views": int(row.get("views") or 0),
        "resume": int(row.get("resume") or 0),
        "chess_people": int(row.get("chess") or 0),
        "spacetime_people": int(row.get("spacetime") or 0),
        "by_ref": refs,
        "company_refs": [r for r in refs if r not in ("(none)", *CHANNEL_REFS)],
    }


def fetch_summary(client: httpx.Client | None = None) -> dict[str, Any]:
    totals_q, by_ref_q = summary_hogql()
    return summarize(query(totals_q, client), query(by_ref_q, client))


def format_section(summary: dict[str, Any]) -> str:
    if not summary["people"] and not summary["resume"]:
        return "Site (last 24 h)\nNo visitors."
    refs = " · ".join(f"{ref} {n}" for ref, n in summary["by_ref"].items())
    lines = [
        "Site (last 24 h)",
        f"Visitors: {summary['people']} ({summary['views']} page views)",
        f"By ref: {refs}" if refs else "",
        f"Resume downloads: {summary['resume']}",
        f"Chess: {summary['chess_people']} people · "
        f"Spacetime: {summary['spacetime_people']} people",
    ]
    if summary["company_refs"]:
        lines.append(f"Companies: {', '.join(summary['company_refs'])}")
    return "\n".join(line for line in lines if line)


def record_day(state: dict[str, Any], day: str, summary: dict[str, Any]) -> dict[str, Any]:
    """Add one day's summary, keeping the last KEEP_DAYS days."""
    days = dict(state.get("days") or {})
    days[day] = summary
    new_state = dict(state)
    new_state["days"] = dict(sorted(days.items())[-KEEP_DAYS:])
    return new_state
