"""Telegram side of research: run jobs in the background, confirm before writing.

Matches to places already in the tree are linked straight away (and the
spoken name added as an alias, so it matches directly next time). New
places are proposals: each gets a confirm card, and nothing lands in the
places tree until the user taps Add. Adding a place also links the
journal items that mentioned it, then offers to rank it.

Research calls take tens of seconds, so they run in a worker thread
(`asyncio.to_thread`) off the event loop, and every entry point checks
the monthly budget first.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import re
import uuid
from datetime import date, datetime, timedelta
from typing import Any

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Message, Update
from telegram.ext import ContextTypes

from daylog import brief, goals, llm, rank_flow, rankings, research, trail
from daylog.places import PlaceIndex
from daylog.tg import allowed_user_id, chunks, is_authorized, tz, vault
from daylog.vault import Vault, VaultError

logger = logging.getLogger(__name__)

_BATCH_KEY = "research_batches"
SPOT_KINDS = ("surf_spot", "wind_spot", "dive_site", "hike", "viewpoint", "beach")
# Fewer known spots than this in a region the user just arrived in triggers
# an 'arrive' research run.
ARRIVE_THRESHOLD = 3
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


def _new_batch(
    context: ContextTypes.DEFAULT_TYPE, findings: research.Findings, days: list[date]
) -> str:
    batch_id = uuid.uuid4().hex[:10]
    # bot_data, not user_data: batches are created from background jobs with
    # no user context, and this is a single-user bot anyway.
    context.bot_data.setdefault(_BATCH_KEY, {})[batch_id] = {
        "proposals": findings.new_places,
        "days": [d.isoformat() for d in days],
        "created": {},
    }
    return batch_id


def _card_text(index: PlaceIndex, batch: dict[str, Any], i: int) -> str:
    p = batch["proposals"][i]
    by_ref = {str(q.get("ref")): str(q["name"]) for q in batch["proposals"]}
    line = research.summarize_proposal(index, p, by_ref)
    mention = f'\nYou said: "{p["mention"]}"' if p.get("mention") else ""
    facts = p.get("facts") or {}
    fact_lines = "".join(f"\n· {k}: {val}" for k, val in facts.items())
    desc = f"\n\n{p['description']}" if p.get("description") else ""
    return f"📍 New place: {line}{mention}{desc}{fact_lines}"


def _card_keyboard(batch_id: str, i: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("Add", callback_data=f"place:add:{batch_id}:{i}"),
                InlineKeyboardButton("Skip", callback_data=f"place:skip:{batch_id}:{i}"),
            ]
        ]
    )


async def send_cards(
    context: ContextTypes.DEFAULT_TYPE,
    chat_id: int,
    findings: research.Findings,
    days: list[date],
    headline: str,
    bulk: bool = False,
) -> None:
    """One card per proposal — or, for a big `bulk` batch, one Add-all card."""
    v = vault()
    index = PlaceIndex(v.read_places())
    text = headline
    if findings.summary:
        text += f"\n\n{findings.summary}"
    if not findings.new_places:
        await context.bot.send_message(chat_id=chat_id, text=text)
        return
    batch_id = _new_batch(context, findings, days)
    store = context.bot_data[_BATCH_KEY][batch_id]
    if bulk and len(findings.new_places) > 3:
        by_ref = {str(q.get("ref")): str(q["name"]) for q in findings.new_places}
        lines = [
            f"{i + 1}. {research.summarize_proposal(index, p, by_ref)}"
            for i, p in enumerate(findings.new_places)
        ]
        keyboard = InlineKeyboardMarkup(
            [
                [
                    InlineKeyboardButton("Add all", callback_data=f"place:addall:{batch_id}:0"),
                    InlineKeyboardButton("Review each", callback_data=f"place:review:{batch_id}:0"),
                ],
                [InlineKeyboardButton("Skip all", callback_data=f"place:skipall:{batch_id}:0")],
            ]
        )
        for chunk in chunks(text + "\n\n" + "\n".join(lines)):
            await context.bot.send_message(chat_id=chat_id, text=chunk, reply_markup=keyboard)
        return
    await context.bot.send_message(chat_id=chat_id, text=text)
    for i in range(len(findings.new_places)):
        await context.bot.send_message(
            chat_id=chat_id,
            text=_card_text(index, store, i),
            reply_markup=_card_keyboard(batch_id, i),
        )


async def handle_place_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_authorized(update):
        return
    query = update.callback_query
    assert query is not None and query.data is not None
    await query.answer()
    _, action, batch_id, idx = query.data.split(":", 3)
    store = context.bot_data.get(_BATCH_KEY, {})
    batch = store.get(batch_id)
    if batch is None:
        await query.edit_message_text("These suggestions expired (the bot restarted).")
        return
    i = int(idx)
    v = vault()

    if action in ("skip", "skipall"):
        what = "all" if action == "skipall" else batch["proposals"][i]["name"]
        await query.edit_message_text(f"Skipped {what}.")
        return
    if action == "review":
        await query.edit_message_text("Reviewing one by one:")
        index = PlaceIndex(v.read_places())
        for j in range(len(batch["proposals"])):
            await context.bot.send_message(
                chat_id=allowed_user_id(),
                text=_card_text(index, batch, j),
                reply_markup=_card_keyboard(batch_id, j),
            )
        return

    indexes = list(range(len(batch["proposals"]))) if action == "addall" else [i]
    try:
        new_ids = add_proposals(v, batch, indexes)
    except VaultError:
        logger.exception("adding places failed")
        await query.edit_message_text("Saving failed — check bot logs. Nothing was added.")
        return
    index = PlaceIndex(v.read_places())
    names = ", ".join(index.path_name(pid) for pid in new_ids) or "already added"
    await query.edit_message_text(f"Added: {names}")

    # Offer to rank anything rankable the user has actually been to.
    if query.message is not None and isinstance(query.message, Message):
        current = v.read_yaml(rank_flow.RANKINGS_FILE, {})
        journal = v.read_journal_range(date(2000, 1, 1), datetime.now(tz()).date())
        visited = trail.visits({d: e.frontmatter for d, e in journal.items()})
        candidates = [(pid, index.by_id[pid].get("kind")) for pid in new_ids if pid in visited]
        for pid, category in rankings.unranked(current, candidates)[:2]:
            await rank_flow.ask_to_rank(
                query.message, context, pid, category, str(index.by_id[pid].get("name"))
            )


# -- jobs ---------------------------------------------------------------------------


def _where(v: Vault, index: PlaceIndex) -> tuple[Any | None, str]:
    current = brief.current_location(v.read_location())
    node = index.current_place(current)
    label = index.path_name(node["id"]) if node else (current or {}).get("place", "unknown")
    return node, str(label)


async def resolve_days(
    context: ContextTypes.DEFAULT_TYPE, chat_id: int, days: list[date], quiet: bool = True
) -> None:
    """Resolve every unlinked place mention in `days`. Quiet: no message if nothing to do."""
    v = vault()
    mentions: list[research.Mention] = []
    for day in days:
        entry = v.read_journal_entry(day)
        if entry is not None:
            mentions += research.unresolved_mentions(day, entry.frontmatter)
    if not mentions:
        if not quiet:
            await context.bot.send_message(chat_id=chat_id, text="Every place is already linked.")
        return
    if not llm.can_spend(v, RUN_ESTIMATE_USD):
        await context.bot.send_message(
            chat_id=chat_id,
            text=f"Skipping place research — monthly budget (${llm.monthly_budget():.0f}) reached.",
        )
        return
    index = PlaceIndex(v.read_places())
    focus, where = _where(v, index)
    findings = await asyncio.to_thread(
        research.run, research.resolve_job(mentions, where), index, focus, max_searches=6
    )
    linked = apply_matches(v, findings.matches, days)
    llm.flush(v)
    headline = f"Looked up {len(mentions)} place name(s) from your journal."
    if linked:
        headline += "\nLinked: " + "; ".join(linked)
    if findings.unresolved:
        headline += "\nCouldn't place: " + ", ".join(u["mention"] for u in findings.unresolved)
    if findings.new_places or linked or not quiet:
        await send_cards(context, chat_id, findings, days, headline)


async def arrive(context: ContextTypes.DEFAULT_TYPE, chat_id: int, force: bool = False) -> None:
    """Research the region the user is in, if it's thinly known (or `force`)."""
    v = vault()
    index = PlaceIndex(v.read_places())
    focus, where = _where(v, index)
    region = index.region_of(focus["id"]) if focus else None
    known = index.descendants(region["id"], SPOT_KINDS) if region else []
    if len(known) >= ARRIVE_THRESHOLD and not force:
        return
    if not llm.can_spend(v, RUN_ESTIMATE_USD):
        return
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
    await context.bot.send_message(chat_id=chat_id, text=f"Researching what's around {where}...")
    findings = await asyncio.to_thread(research.run, job, index, focus, max_searches=10)
    llm.flush(v)
    await send_cards(
        context,
        chat_id,
        findings,
        [],
        f"Around {where}: {len(findings.new_places)} places found.",
        bulk=True,
    )


async def trip_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/trip — reconstruct the stops of the most recent multi-day trip."""
    if not is_authorized(update):
        return
    message = update.message
    assert message is not None
    v = vault()
    trips = [s for s in trail.stays(v.read_location()) if s.mode == "trip"]
    if not trips:
        await message.reply_text("No trip in your location history (mode: trip) yet.")
        return
    trip = trips[-1]
    end = trip.end or datetime.now(tz()).date()
    entries = v.read_journal_range(trip.start, end)
    days = [(d, e.summary) for d, e in sorted(entries.items())]
    if not llm.can_spend(v, RUN_ESTIMATE_USD):
        await message.reply_text("Monthly research budget reached.")
        return
    await message.reply_text(f"Reconstructing {trip.place} ({trip.start} → {end})...")
    index = PlaceIndex(v.read_places())
    findings = await asyncio.to_thread(
        research.run, research.trip_job(trip.place, days), index, None, max_searches=8
    )
    trip_days = [d for d, _ in days]
    linked = apply_matches(v, findings.matches, trip_days)
    stops: dict[date, list[str]] = {}
    for m in findings.matches:
        with contextlib.suppress(ValueError, KeyError):
            stops.setdefault(date.fromisoformat(m["day"]), []).append(m["place_id"])
    for day, ids in stops.items():
        _add_trip_stops(v, day, ids)
    llm.flush(v)
    headline = f"{trip.place}: proposed stops."
    if linked:
        headline += "\nAlready known: " + "; ".join(linked)
    await send_cards(context, message.chat_id, findings, trip_days, headline)


async def backfill_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/backfill [days] — resolve unlinked place names across recent entries (default 30)."""
    if not is_authorized(update):
        return
    message = update.message
    assert message is not None
    args = context.args or []
    n = int(args[0]) if args and args[0].isdigit() else 30
    today = datetime.now(tz()).date()
    days = [today - timedelta(days=i) for i in range(n)]
    await message.reply_text(f"Checking the last {n} days for unlinked places...")
    await resolve_days(context, message.chat_id, days, quiet=False)


async def explore_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/explore — research the spots around where you are now."""
    if not is_authorized(update):
        return
    message = update.message
    assert message is not None
    await arrive(context, message.chat_id, force=True)


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


async def write_dossier(v: Vault, place_id: str | None, place_name: str) -> tuple[str, str]:
    """Research and save a dossier. Returns (vault path, markdown)."""
    index = PlaceIndex(v.read_places())
    text, _cost = await asyncio.to_thread(research.dossier, place_name, dossier_context(v, index))
    path = dossier_path(place_id or research_slug(place_name))
    v.write_text(path, text + "\n", f"research: {place_name}")
    llm.flush(v)
    return path, text


def research_slug(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-") or "place"


async def research_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/research <place> — write a research file on a place now (uses the API budget)."""
    if not is_authorized(update):
        return
    message = update.message
    assert message is not None
    query = " ".join(context.args or []).strip()
    if not query:
        await message.reply_text("Usage: /research <place>, e.g. /research Mentawai")
        return
    v = vault()
    if not llm.can_spend(v, RUN_ESTIMATE_USD):
        await message.reply_text(
            "Monthly research budget reached — the morning routine still runs."
        )
        return
    index = PlaceIndex(v.read_places())
    matches = index.resolve(query)
    place_id = matches[0] if matches else None
    name = index.path_name(place_id) if place_id else query
    await message.reply_text(f"Researching {name} — this takes a minute or two...")
    try:
        path, text = await write_dossier(v, place_id, name)
    except Exception:
        logger.exception("dossier failed for %s", name)
        await message.reply_text("Research failed — check bot logs.")
        return
    for chunk in chunks(f"{text}\n\n(saved to {path})"):
        await message.reply_text(chunk)


async def usage_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_authorized(update):
        return
    message = update.message
    assert message is not None
    await message.reply_text(llm.format_usage(vault()))
