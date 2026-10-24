"""Read-side helpers: status text, upcoming dates, the trail range, a place
card, the conditions reports and the morning brief.

The web app (webapp/api.py) and the scheduler (scheduler.py) call these.
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from daylog import (
    brief,
    disciplines,
    goals,
    itinerary,
    plan_flow,
    rankings,
    site_flow,
    trail,
    wishlist_flow,
)
from daylog.places import PlaceIndex
from daylog.rank_flow import RANKINGS_FILE
from daylog.runtime import tz
from daylog.sources import fly, marine, surf, wind
from daylog.vault import Vault, VaultError

logger = logging.getLogger(__name__)


# -- /status and /upcoming --------------------------------------------------------


def format_goal_line(g: Any) -> str:
    bits = []
    if g.get("metric"):
        bits.append(f"{g.get('progress', 0):g} {g['metric']}")
    if g.get("type") == "hard" and g.get("deadline"):
        bits.append(f"deadline {g['deadline']}")
    elif g.get("target_window"):
        window = g["target_window"]
        if window and window[-1]:
            bits.append(f"target {window[-1]}")

    status = g.get("status", "active")
    status_tag = f" [{status}]" if status != "active" else ""
    line = f"- {g.get('title', g['id'])} ({g.get('type', 'soft')}){status_tag}"
    return f"{line}: {', '.join(bits)}" if bits else line


def format_itinerary_line(e: Any) -> str:
    status = e.get("status", "candidate")
    line = f"- {e.get('place', e['id'])} [{status}] ({e.get('type', 'soft')})"
    entry_date = itinerary.current_date(e)
    if entry_date:
        label = "deadline" if e.get("type") == "hard" else "target"
        line += f": {label} {entry_date}"
    return line


def format_status(goals_data: Any, itinerary_data: Any) -> str:
    goal_lines = [format_goal_line(g) for g in goals_data if g.get("status") != "dropped"]
    itin_lines = [format_itinerary_line(e) for e in itinerary_data if e.get("status") != "dropped"]
    return (
        "Goals:\n"
        + ("\n".join(goal_lines) if goal_lines else "(none)")
        + "\n\nItinerary:\n"
        + ("\n".join(itin_lines) if itin_lines else "(none)")
    )


def upcoming_items(goals_data: Any, itinerary_data: Any, today: date) -> list[str]:
    """One line per active goal/itinerary item with a resolvable date, earliest first.

    Deliberately doesn't hide past-due dates — an active item whose date
    has already passed is exactly the "you forgot about this" signal worth
    surfacing at the top, not filtering out.
    """
    dated: list[tuple[date, str]] = []
    for g in goals_data:
        if g.get("status") in ("done", "dropped"):
            continue
        d_str = goals.current_target_date(g)
        if not d_str:
            continue
        d = date.fromisoformat(d_str)
        kind = "deadline" if g.get("type") == "hard" else "target"
        overdue = " (overdue)" if d < today else ""
        dated.append((d, f"{d.isoformat()}: {g.get('title', g['id'])} — {kind}{overdue}"))
    for e in itinerary_data:
        if e.get("status") in ("done", "dropped"):
            continue
        d_str = itinerary.current_date(e)
        if not d_str:
            continue
        d = date.fromisoformat(d_str)
        kind = "deadline" if e.get("type") == "hard" else e.get("status", "candidate")
        overdue = " (overdue)" if d < today else ""
        dated.append((d, f"{d.isoformat()}: {e.get('place', e['id'])} — {kind}{overdue}"))
    dated.sort(key=lambda item: item[0])
    return [label for _, label in dated]


# -- /trail and /place ----------------------------------------------------------


def trail_range(args: list[str], today: date, location_data: Any) -> tuple[date, date]:
    """/trail -> last 30 days; /trail 60 -> last 60; /trail all -> since the first stay."""
    if args and args[0].lower() == "all":
        starts = [s.start for s in trail.stays(location_data)]
        return (min(starts) if starts else today - timedelta(days=30)), today
    days = int(args[0]) if args and args[0].isdigit() else 30
    return today - timedelta(days=days - 1), today


def trail_text(v: Vault, args: list[str], today: date) -> str:
    location_data = v.read_location()
    start, end = trail_range(args, today, location_data)
    entries = v.read_journal_range(start, end)
    days = trail.build(
        location_data,
        {d: e.frontmatter for d, e in entries.items()},
        {d: e.summary for d, e in entries.items()},
        start,
        end,
    )
    return trail.format_trail(days, PlaceIndex(v.read_places()))


def find_places(index: PlaceIndex, query: str) -> list[str]:
    """Place ids for a name: exact (and alias) matches first, else a substring of the name."""
    matches = index.resolve(query)
    if not matches:
        needle = query.lower()
        matches = [i for i, n in index.by_id.items() if needle in str(n.get("name", "")).lower()]
    return matches


def format_place_card(
    index: PlaceIndex, node: Any, visited: list[date], rank_line: str | None
) -> str:
    lines = [f"{node.get('name')} — {node.get('kind')}", index.path_name(node["id"])]
    if node.get("description"):
        lines += ["", str(node["description"]).strip()]
    facts = node.get("facts") or {}
    if facts:
        lines += [""] + [f"{k}: {v}" for k, v in facts.items() if v]
    if node.get("notes"):
        lines += ["", str(node["notes"]).strip()]
    if rank_line:
        lines += ["", rank_line]
    if visited:
        lines += ["", "Visited: " + ", ".join(d.isoformat() for d in visited)]
    for note in node.get("my_notes") or []:
        lines.append(f"- {note.get('date')}: {note.get('text')}")
    children = index.descendants(node["id"])
    if children:
        lines += ["", "Inside: " + ", ".join(str(c.get("name")) for c in children[:30])]
    return "\n".join(lines)


def place_card_text(v: Vault, index: PlaceIndex, node: Any, today: date) -> str:
    """/place's card: the node, where it ranks, and the days it was visited."""
    entries = v.read_journal_range(date(2000, 1, 1), today)
    visited = trail.visits({d: e.frontmatter for d, e in entries.items()}, index)
    category = rankings.category_for(node.get("kind"))
    rank_line = (
        rankings.describe(v.read_yaml(RANKINGS_FILE, {}), category, node["id"])
        if category
        else None
    )
    return format_place_card(index, node, visited.get(node["id"], []), rank_line)


# -- conditions --------------------------------------------------------------------


def relevant_spots(current: dict[str, Any] | None, places_data: Any) -> list[dict[str, Any]]:
    """Current location plus every surf/wind spot with coordinates in its region.

    Comparing multiple nearby spots (not just where the user happens to be
    standing) is the point — swell or wind can be building somewhere better
    a short trip away.
    """
    if not current:
        return []
    index = PlaceIndex(list(places_data or []))
    here = index.current_place(current)
    # A stay linked by place_id usually carries no coordinates of its own;
    # the place node (or its nearest located ancestor) has them.
    lat, lon = current.get("lat"), current.get("lon")
    if (lat is None or lon is None) and here is not None:
        located = index.with_coordinates([here])
        if located:
            lat, lon = located[0]["lat"], located[0]["lon"]
    spots = []
    if lat is not None and lon is not None:
        spots.append({"name": current.get("place", "current location"), "lat": lat, "lon": lon})
    for spot in index.forecast_spots(here):
        spots.append(
            {"name": spot.get("name", "nearby spot"), "lat": spot["lat"], "lon": spot["lon"]}
        )
    return spots


def fetch_conditions(
    current: dict[str, Any] | None,
    places_data: Any,
    zone: ZoneInfo,
    fetch_fn: Callable[[float, float, str], str | None],
) -> str | None:
    blocks = []
    for spot in relevant_spots(current, places_data):
        forecast = fetch_fn(spot["lat"], spot["lon"], str(zone))
        if forecast:
            blocks.append(f"{spot['name']}:\n{forecast}")
    return "\n\n".join(blocks) if blocks else None


def fetch_marine_forecast(
    current: dict[str, Any] | None, places_data: Any, zone: ZoneInfo
) -> str | None:
    return fetch_conditions(current, places_data, zone, marine.fetch_forecast)


def fetch_wind_forecast(
    current: dict[str, Any] | None, places_data: Any, zone: ZoneInfo
) -> str | None:
    return fetch_conditions(current, places_data, zone, wind.fetch_forecast)


def conditions_reports(
    vault: Any, current: dict[str, Any] | None, places_data: Any
) -> tuple[str | None, str | None]:
    """(rated surf report, flyability report) for the current region — free, no LLM."""
    index = PlaceIndex(list(places_data or []))
    here = index.current_place(current)
    profile = vault.read_profile() or {}
    comfort = profile.get("surf_comfort_face_m") if isinstance(profile, dict) else None
    rules = profile.get("fly_rules") if isinstance(profile, dict) else None
    zone = str(tz())
    surf_spots = index.with_coordinates(index.region_nodes(here, ("surf_spot",)))
    fly_sites = index.with_coordinates(index.region_nodes(here, ("fly_site",)))
    rated_surf = surf.report(surf_spots, zone, comfort_face=comfort)
    rated_fly = fly.report(fly_sites, zone, rules)
    return rated_surf, rated_fly


def surf_text(v: Vault) -> str:
    current = brief.current_location(v.read_location())
    rated, _ = conditions_reports(v, current, v.read_places())
    return rated or "No surf spots with a profile around here yet."


def fly_text(v: Vault) -> str:
    current = brief.current_location(v.read_location())
    _, rated = conditions_reports(v, current, v.read_places())
    return rated or "No launch sites with a profile around here yet."


def _region(v: Vault) -> tuple[PlaceIndex, Any, dict[str, Any]]:
    places_data = v.read_places()
    index = PlaceIndex(list(places_data or []))
    here = index.current_place(brief.current_location(v.read_location()))
    profile = v.read_profile() or {}
    return index, here, profile if isinstance(profile, dict) else {}


def surf_data(v: Vault) -> list[dict[str, Any]]:
    """The rated surf forecast for the current region, as numbers (web app)."""
    index, here, profile = _region(v)
    spots = index.with_coordinates(index.region_nodes(here, ("surf_spot",)))
    return surf.report_data(spots, str(tz()), comfort_face=profile.get("surf_comfort_face_m"))


def fly_data(v: Vault) -> list[dict[str, Any]]:
    """Paragliding flyability for the current region, as numbers (web app)."""
    index, here, profile = _region(v)
    sites = index.with_coordinates(index.region_nodes(here, ("fly_site",)))
    return fly.report_data(sites, str(tz()), profile.get("fly_rules"))


# -- the morning brief -----------------------------------------------------------


@dataclass
class Brief:
    date: date
    text: str


_brief_lock = threading.Lock()
_last_brief: Brief | None = None


BRIEFS = "briefs"


def brief_path(day: date) -> str:
    return f"{BRIEFS}/{day.isoformat()}.md"


def last_brief(v: Vault | None = None) -> Brief | None:
    """The latest brief: the one built in this process, else (given the vault)
    the newest saved under briefs/, so a restart doesn't lose it."""
    with _brief_lock:
        if _last_brief is not None or v is None:
            return _last_brief
    docs = v.list_docs(BRIEFS)
    if not docs:
        return None
    try:
        day = date.fromisoformat(docs[-1].rsplit("/", 1)[-1].removesuffix(".md"))
    except ValueError:
        return None
    text = v.read_text(docs[-1])
    return Brief(day, text.strip()) if text else None


def build_brief(v: Vault, today: date) -> str:
    """The morning brief, with the Site block when analytics are set up.

    Raises if the brief itself fails; a Site failure is logged and left out.
    The result is remembered for last_brief() and saved to briefs/YYYY-MM-DD.md
    (a failed save is logged).
    """
    global _last_brief
    location_data = v.read_location()
    places_data = v.read_places()

    current = brief.current_location(location_data)
    rated_surf, rated_fly = conditions_reports(v, current, places_data)
    # Spots with a surf profile get per-spot ratings; the plain daily
    # numbers are the fallback for regions nobody has profiled yet.
    marine_forecast = rated_surf or fetch_marine_forecast(current, places_data, tz())
    wind_forecast = fetch_wind_forecast(current, places_data, tz())

    text = brief.generate_brief(
        today=today,
        goals_data=v.read_goals(),
        itinerary_data=v.read_itinerary(),
        places_data=places_data,
        location_data=location_data,
        profile_data=v.read_profile(),
        recent_journal=brief.recent_journal_summaries(v, today),
        marine_forecast=marine_forecast,
        wind_forecast=wind_forecast,
        fly_forecast=rated_fly,
        dossier_digests={
            name: wishlist_flow.dossier_digest(text)
            for name, text in wishlist_flow.dossiers_for(v, v.read_itinerary()).items()
        },
        morning_research=v.read_text(f"research/daily/{today.isoformat()}.md"),
        current_plan=plan_flow.latest_plan(v),
        principles=v.read_principles(),
        disciplines=disciplines.summary(v, today),
    )
    try:
        site = site_flow.brief_section(v, today)
    except Exception:
        logger.exception("site section failed")
        site = None
    if site:
        text += "\n\n" + site
    with _brief_lock:
        _last_brief = Brief(today, text)
    try:
        v.write_text(brief_path(today), text.rstrip() + "\n", f"brief: {today.isoformat()}")
    except VaultError:
        logger.exception("saving the brief failed")
    return text
