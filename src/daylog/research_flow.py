"""Research's vault side: link what's known, confirm before writing anything new.

Matches to places already in the tree are linked straight away (and the
spoken name added as an alias, so it matches directly next time). New
places are proposals: each becomes a confirm card in the web app, and
nothing lands in the places tree until the user taps Add. Adding a place
also links the journal items that mentioned it.

Every paid entry point checks the monthly budget first.
"""

from __future__ import annotations

import contextlib
import logging
import re
import threading
from dataclasses import dataclass
from datetime import date, datetime
from typing import Any

from daylog import brief, goals, llm, research, trail
from daylog.places import PlaceIndex
from daylog.runtime import tz
from daylog.vault import Vault

logger = logging.getLogger(__name__)

SPOT_KINDS = ("surf_spot", "wind_spot", "dive_site", "hike", "viewpoint", "beach")
# Fewer known spots than this in a region the user just arrived in triggers
# an 'arrive' research run.
ARRIVE_THRESHOLD = 3
# A place lookup needs a few searches, not a deep dive.
RESOLVE_SEARCHES = 3

# (day, mention) pairs being looked up right now: two notes about the same day
# once started two identical lookups at the same moment.
_in_flight: set[tuple[date, str]] = set()
_in_flight_lock = threading.Lock()
# Rough upper bound of one research run, for the budget check.
RUN_ESTIMATE_USD = 0.40


def _norm(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", text.lower()).strip()


# -- applying findings -----------------------------------------------------------


def _add_alias(node: Any, mention: str) -> bool:
    known = {_norm(str(n)) for n in [node.get("name", ""), *(node.get("aliases") or [])]}
    if _norm(mention) in known:
        return False
    aliases = node.get("aliases")
    if aliases is None:
        aliases = []
        node["aliases"] = aliases
    aliases.append(mention)
    return True


def apply_matches(v: Vault, matches: list[dict[str, Any]], days: list[date]) -> list[str]:
    """Link matched mentions in the given days and learn the spoken name as an alias."""
    if not matches:
        return []
    files = v.read_place_files()
    by_id = {n["id"]: (stem, n) for stem, nodes in files.items() for n in nodes}
    changed: set[str] = set()
    for m in matches:
        stem, node = by_id[m["place_id"]]
        if _add_alias(node, m["mention"]):
            changed.add(stem)
    if changed:
        v.write_place_files(files, changed, "places: learn spoken names as aliases")
    mention_to_id = {m["mention"]: m["place_id"] for m in matches}
    _link_days(v, days, mention_to_id)
    return [f"{m['mention']} → {by_id[m['place_id']][1].get('name')}" for m in matches]


def _mark_looked_up(v: Vault, mentions: list[research.Mention]) -> None:
    """Record that these mentions were looked up, so no later note repeats it."""
    by_day: dict[date, set[str]] = {}
    for m in mentions:
        by_day.setdefault(m.day, set()).add(m.text)
    for day, texts in sorted(by_day.items()):
        entry = v.read_journal_entry(day)
        if entry is not None and research.mark_looked_up(entry.frontmatter, texts, date.today()):
            v.rewrite_journal_entry(
                day, entry.frontmatter, f"journal: {day.isoformat()} (places looked up)"
            )


def _link_days(v: Vault, days: list[date], mention_to_id: dict[str, str]) -> None:
    for day in sorted(set(days)):
        entry = v.read_journal_entry(day)
        if entry is None:
            continue
        if research.link_mentions(entry.frontmatter, mention_to_id):
            v.rewrite_journal_entry(
                day, entry.frontmatter, f"journal: {day.isoformat()} link places"
            )


def _chain(proposals: list[dict[str, Any]], i: int, created: dict[str, str]) -> list[int]:
    """Indexes to add for proposal i: its not-yet-created parent_refs first, then itself."""
    by_ref = {p.get("ref"): j for j, p in enumerate(proposals)}
    order: list[int] = []
    j: int | None = i
    seen: set[int] = set()
    while j is not None and j not in seen:
        seen.add(j)
        order.insert(0, j)
        ref = proposals[j].get("parent_ref")
        j = by_ref.get(ref) if ref and ref not in created else None
    return order


def add_proposals(v: Vault, batch: dict[str, Any], indexes: list[int]) -> list[str]:
    """Write the chosen proposals (plus any parents they need), link the journal.

    Returns the ids created. `batch["created"]` (ref -> id) is updated so a
    later card in the same batch can parent onto something added earlier.
    """
    proposals: list[dict[str, Any]] = batch["proposals"]
    created: dict[str, str] = batch.setdefault("created", {})
    todo: list[int] = []
    for i in indexes:
        for j in _chain(proposals, i, created):
            ref = proposals[j].get("ref")
            if j not in todo and ref not in created:
                todo.append(j)
    if not todo:
        return []
    chosen = []
    for j in todo:
        p = dict(proposals[j])
        if p.get("parent_ref") in created:
            p["parent_id"] = created[p["parent_ref"]]
            p.pop("parent_ref")
        chosen.append(p)

    files = v.read_place_files()
    before = {stem: len(nodes) for stem, nodes in files.items()}
    new_ids = research.apply_new_places(files, chosen, datetime.now(tz()).date())
    changed = {stem for stem, nodes in files.items() if len(nodes) != before.get(stem, 0)}
    names = ", ".join(str(p["name"]) for p in chosen)
    v.write_place_files(files, changed, f"places: add {names}")
    created.update(new_ids)

    mention_to_id: dict[str, str] = {}
    trip_days: dict[date, list[str]] = {}
    for p in chosen:
        new_id = new_ids.get(str(p.get("ref")))
        if not new_id:
            continue
        if p.get("mention"):
            mention_to_id[str(p["mention"])] = new_id
        if p.get("day"):
            with contextlib.suppress(ValueError):
                trip_days.setdefault(date.fromisoformat(str(p["day"])), []).append(new_id)
    if mention_to_id:
        _link_days(v, [date.fromisoformat(d) for d in batch.get("days", [])], mention_to_id)
    for day, ids in trip_days.items():
        _add_trip_stops(v, day, ids)
    return list(new_ids.values())


def _add_trip_stops(v: Vault, day: date, place_ids: list[str]) -> None:
    """Record confirmed trip stops on their day, unless already linked there."""
    entry = v.read_journal_entry(day)
    if entry is None:
        return
    present = set(trail.day_place_ids(entry.frontmatter))
    activities = entry.frontmatter.get("activities") or []
    added = False
    for place_id in place_ids:
        if place_id not in present:
            activities.append({"type": "visit", "place": place_id})
            present.add(place_id)
            added = True
    if added:
        entry.frontmatter["activities"] = activities
        v.rewrite_journal_entry(day, entry.frontmatter, f"journal: {day.isoformat()} trip stops")


# -- cards --------------------------------------------------------------------------


def new_batch(findings: research.Findings, days: list[date]) -> dict[str, Any]:
    """The proposals of one research run, shared by its cards (see add_proposals)."""
    return {
        "proposals": findings.new_places,
        "days": [d.isoformat() for d in days],
        "created": {},
    }


def bulk_lines(index: PlaceIndex, findings: research.Findings) -> list[str]:
    by_ref = {str(q.get("ref")): str(q["name"]) for q in findings.new_places}
    return [
        f"{i + 1}. {research.summarize_proposal(index, p, by_ref)}"
        for i, p in enumerate(findings.new_places)
    ]


def card_text(index: PlaceIndex, batch: dict[str, Any], i: int) -> str:
    p = batch["proposals"][i]
    by_ref = {str(q.get("ref")): str(q["name"]) for q in batch["proposals"]}
    line = research.summarize_proposal(index, p, by_ref)
    mention = f'\nYou said: "{p["mention"]}"' if p.get("mention") else ""
    facts = p.get("facts") or {}
    fact_lines = "".join(f"\n· {k}: {val}" for k, val in facts.items())
    desc = f"\n\n{p['description']}" if p.get("description") else ""
    return f"📍 New place: {line}{mention}{desc}{fact_lines}"


# -- jobs ---------------------------------------------------------------------------


def _where(v: Vault, index: PlaceIndex) -> tuple[Any | None, str]:
    current = brief.current_location(v.read_location())
    node = index.current_place(current)
    label = index.path_name(node["id"]) if node else (current or {}).get("place", "unknown")
    return node, str(label)


def lookup_mentions(
    v: Vault, days: list[date], quiet: bool = True
) -> tuple[str | None, research.Findings | None]:
    """Resolve the unlinked place mentions in `days`: link the known ones and
    return (headline, findings) whose new places become Add cards. Findings is
    None when there's nothing to show (the headline, if any, says why).

    `quiet` is the automatic run after a note: it only matches names against
    places already known, which is free. Searching the web for a new place
    costs money and runs only when asked (/backfill, the assistant) — a
    passing mention of a 1am shisha bar once cost ~$4 in automatic lookups."""
    mentions: list[research.Mention] = []
    for day in days:
        entry = v.read_journal_entry(day)
        if entry is not None:
            mentions += research.unresolved_mentions(day, entry.frontmatter)
    if not mentions:
        return (None if quiet else "Every place is already linked or looked up."), None
    index = PlaceIndex(v.read_places())
    focus, _ = _where(v, index)
    region = index.region_of(focus["id"]) if focus else None
    known = [
        {"mention": m.text, "place_id": ids[0]}
        for m in mentions
        if (ids := index.resolve(m.text, within=region["id"] if region else None))
    ]
    linked_free = apply_matches(v, known, days) if known else []
    if quiet:
        return None, None
    matched = {k["mention"] for k in known}
    mentions = [m for m in mentions if m.text not in matched]
    with _in_flight_lock:
        mentions = [m for m in mentions if (m.day, m.text) not in _in_flight]
        _in_flight.update((m.day, m.text) for m in mentions)
    if not mentions:
        if linked_free:
            return "Linked: " + "; ".join(linked_free), None
        return "Every place is already linked or looked up.", None
    try:
        if not llm.can_spend(v, RUN_ESTIMATE_USD):
            return (
                f"Skipping place research — monthly budget (${llm.monthly_budget():.0f}) reached.",
                None,
            )
        index = PlaceIndex(v.read_places())
        focus, where = _where(v, index)
        findings = research.run(
            research.resolve_job(mentions, where), index, focus, max_searches=RESOLVE_SEARCHES
        )
        linked = linked_free + apply_matches(v, findings.matches, days)
        _mark_looked_up(v, mentions)
        llm.flush(v)
    finally:
        with _in_flight_lock:
            _in_flight.difference_update((m.day, m.text) for m in mentions)
    headline = f"Looked up {len(mentions)} place name(s) from your journal."
    if linked:
        headline += "\nLinked: " + "; ".join(linked)
    if findings.unresolved:
        headline += "\nCouldn't place: " + ", ".join(u["mention"] for u in findings.unresolved)
    return headline, findings


@dataclass
class Job:
    """A research run worked out but not started: what it's about and its prompt."""

    where: str
    job: str
    index: PlaceIndex
    focus: Any | None
    max_searches: int


def plan_arrive(v: Vault, force: bool = False) -> Job | None:
    """The 'arrive' run for the region the user is in, or None when it's already
    well known (unless `force`) or the budget is spent."""
    index = PlaceIndex(v.read_places())
    focus, where = _where(v, index)
    region = index.region_of(focus["id"]) if focus else None
    known = index.descendants(region["id"], SPOT_KINDS) if region else []
    if len(known) >= ARRIVE_THRESHOLD and not force:
        return None
    if not llm.can_spend(v, RUN_ESTIMATE_USD):
        return None
    activities = list((region or {}).get("activities") or [])
    profile = v.read_profile() or {}
    activities += (
        [str(a) for a in profile.get("activities", [])] if isinstance(profile, dict) else []
    )
    job = research.arrive_job(
        index.path_name(region["id"]) if region else where,
        activities,
        [str(k.get("name")) for k in known],
    )
    return Job(where, job, index, focus, 10)


def run_job(v: Vault, job: Job) -> research.Findings:
    findings = research.run(job.job, job.index, job.focus, max_searches=job.max_searches)
    llm.flush(v)
    return findings


@dataclass
class TripJob:
    place: str
    started: str
    days: list[date]
    job: str


def plan_trip(v: Vault, today: date) -> TripJob | str:
    """The reconstruction of the most recent trip, or a message saying why not."""
    trips = [s for s in trail.stays(v.read_location()) if s.mode == "trip"]
    if not trips:
        return "No trip in your location history (mode: trip) yet."
    trip = trips[-1]
    end = trip.end or today
    entries = v.read_journal_range(trip.start, end)
    days = [(d, e.summary) for d, e in sorted(entries.items())]
    if not llm.can_spend(v, RUN_ESTIMATE_USD):
        return "Monthly research budget reached."
    return TripJob(
        trip.place,
        f"Reconstructing {trip.place} ({trip.start} → {end})...",
        [d for d, _ in days],
        research.trip_job(trip.place, days),
    )


def run_trip(v: Vault, job: TripJob) -> tuple[str, research.Findings]:
    """Run it: link known stops on their days; returns (headline, findings)."""
    index = PlaceIndex(v.read_places())
    findings = research.run(job.job, index, None, max_searches=8)
    linked = apply_matches(v, findings.matches, job.days)
    stops: dict[date, list[str]] = {}
    for m in findings.matches:
        with contextlib.suppress(ValueError, KeyError):
            stops.setdefault(date.fromisoformat(m["day"]), []).append(m["place_id"])
    for day, ids in stops.items():
        _add_trip_stops(v, day, ids)
    llm.flush(v)
    headline = f"{job.place}: proposed stops."
    if linked:
        headline += "\nAlready known: " + "; ".join(linked)
    return headline, findings


def dossier_context(v: Vault, index: PlaceIndex) -> str:
    _, where = _where(v, index)
    goal_lines = [f"- {g['title']} ({g['type']})" for g in goals.active_summary(v.read_goals())]
    profile = v.read_profile() or {}
    profile_lines = (
        [f"- {k}: {val}" for k, val in profile.items()] if isinstance(profile, dict) else []
    )
    return (
        f"They are currently in: {where}\n\n"
        f"Their goals:\n{chr(10).join(goal_lines) or '(none)'}\n\n"
        f"Profile:\n{chr(10).join(profile_lines) or '(none)'}"
    )


def dossier_path(place_id: str) -> str:
    return f"research/{place_id}.md"


def dossier_now(v: Vault, place_id: str | None, place_name: str) -> tuple[str, str]:
    """Research and save a dossier (blocking). Returns (vault path, markdown)."""
    index = PlaceIndex(v.read_places())
    text, _cost = research.dossier(place_name, dossier_context(v, index))
    path = dossier_path(place_id or research_slug(place_name))
    v.write_text(path, text + "\n", f"research: {place_name}")
    llm.flush(v)
    return path, text


def place_target(v: Vault, query: str) -> tuple[str | None, str]:
    """(place id or None, the name to research) for what the user typed."""
    index = PlaceIndex(v.read_places())
    matches = index.resolve(query)
    place_id = matches[0] if matches else None
    return place_id, (index.path_name(place_id) if place_id else query)


def research_slug(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-") or "place"
