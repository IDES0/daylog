"""The wishlist: itinerary.yaml's candidate/planned destinations, with research.

itinerary.yaml keeps its name and shape (calendar feed, goals-style dates
and hard-deadline confirmation all still apply) but now holds intentions
only: places the user wants to go, each optionally linked to a place id,
the goals it serves (`why`), and a research file. A new entry is queued
for research (`research: queued`); the scheduled morning routine picks the
queue up for free, or /want's "Research now" button runs it immediately
against the API budget.
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Any

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import ContextTypes

from daylog import itinerary, llm, research_flow
from daylog.places import PlaceIndex
from daylog.tg import allowed_user_id, chunks, is_authorized, tz, vault
from daylog.vault import Vault, VaultError

logger = logging.getLogger(__name__)

ACTIVE = ("candidate", "planned")


def dossier_digest(
    markdown: str, sections: tuple[str, ...] = ("When", "Events"), limit: int = 700
) -> str:
    """Just the named `## ` sections of a research file, each trimmed — enough
    for the brief or planner to weigh timing without reading the whole file."""
    out: list[str] = []
    current: str | None = None
    buf: list[str] = []

    def flush() -> None:
        if current in sections and buf:
            body = "\n".join(buf).strip()
            out.append(f"{current}: {body[:limit]}")

    for line in markdown.splitlines():
        if line.startswith("## "):
            flush()
            current = line[3:].strip()
            buf = []
        elif current is not None:
            buf.append(line)
    flush()
    return "\n".join(out)


def active_entries(itinerary_data: Any) -> list[Any]:
    return [e for e in itinerary_data or [] if e.get("status") in ACTIVE]


def dossiers_for(v: Vault, itinerary_data: Any) -> dict[str, str]:
    """entry place label -> full research markdown, for active entries that have one."""
    out: dict[str, str] = {}
    for entry in active_entries(itinerary_data):
        path = entry.get("dossier") or research_flow.dossier_path(
            str(entry.get("place_id") or entry["id"])
        )
        text = v.read_text(str(path))
        if text:
            out[str(entry.get("place", entry["id"]))] = text
    return out


def format_wishlist(itinerary_data: Any, index: PlaceIndex) -> str:
    lines = []
    for e in active_entries(itinerary_data):
        where = index.path_name(e["place_id"]) if e.get("place_id") in index.by_id else ""
        why = f" · for {', '.join(e['why'])}" if e.get("why") else ""
        when = itinerary.current_date(e)
        when_bit = f" · by {when}" if when else ""
        research = e.get("research", "none")
        dossier = f" ({e['dossier']})" if e.get("dossier") else ""
        lines.append(
            f"- {e.get('place', e['id'])} [{e.get('status')}]{why}{when_bit}\n"
            f"  {where + ' · ' if where else ''}research: {research}{dossier}"
        )
    return "\n".join(lines) if lines else "Wishlist is empty — /want <place> to add one."


def add_wish(v: Vault, query: str, today: Any) -> tuple[Any, bool]:
    """Add (or re-activate) a candidate for `query`. Returns (entry, created)."""
    index = PlaceIndex(v.read_places())
    matches = index.resolve(query)
    place_id = matches[0] if matches else None
    data = v.read_itinerary()
    for entry in data:
        same_place = place_id and entry.get("place_id") == place_id
        same_name = str(entry.get("place", "")).lower() == query.lower()
        if same_place or same_name:
            if entry.get("status") not in ACTIVE:
                entry["status"] = "candidate"
            itinerary.queue_research(entry)
            v.write_itinerary(data, f"itinerary: want {entry.get('place')}")
            return entry, False
    name = str(index.by_id[place_id]["name"]) if place_id else query
    change: dict[str, Any] = {"place": name, "status": "candidate"}
    if place_id:
        change["place_id"] = place_id
    applied, _ = itinerary.apply_itinerary_changes(data, [change], on=today)
    v.write_itinerary(data, f"itinerary: want {name}")
    return itinerary.find_entry(data, applied[0].id), True


async def want_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/want <place> — add a destination to the wishlist and queue research on it."""
    if not is_authorized(update):
        return
    message = update.message
    assert message is not None
    query = " ".join(context.args or []).strip()
    if not query:
        await message.reply_text("Usage: /want <place>, e.g. /want Mentawai")
        return
    v = vault()
    try:
        entry, created = add_wish(v, query, datetime.now(tz()).date())
    except VaultError:
        logger.exception("want failed for %s", query)
        await message.reply_text("Saving failed — check bot logs.")
        return
    verb = "Added" if created else "Already on the list —"
    keyboard = InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("Research now", callback_data=f"want:now:{entry['id']}")],
            [
                InlineKeyboardButton(
                    "Leave it for the morning routine", callback_data="want:later:x"
                )
            ],
        ]
    )
    await message.reply_text(
        f"{verb} {entry.get('place')}. Research is queued for the morning routine (free), "
        "or run it now (API budget, a minute or two).",
        reply_markup=keyboard,
    )


async def handle_want_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_authorized(update):
        return
    query = update.callback_query
    assert query is not None and query.data is not None
    await query.answer()
    _, action, entry_id = query.data.split(":", 2)
    if action == "later":
        await query.edit_message_text("Queued — tomorrow's morning routine will research it.")
        return
    v = vault()
    if not llm.can_spend(v, research_flow.RUN_ESTIMATE_USD):
        await query.edit_message_text("Monthly budget reached — left queued for the routine.")
        return
    data = v.read_itinerary()
    entry = itinerary.find_entry(data, entry_id)
    if entry is None:
        await query.edit_message_text("That wishlist entry is gone.")
        return
    index = PlaceIndex(v.read_places())
    place_id = entry.get("place_id")
    name = index.path_name(place_id) if place_id in index.by_id else str(entry.get("place"))
    await query.edit_message_text(f"Researching {name}...")
    try:
        path, text = await research_flow.write_dossier(v, place_id, name)
    except Exception:
        logger.exception("dossier failed for %s", name)
        await query.edit_message_text(f"Research on {name} failed — still queued.")
        return
    data = v.read_itinerary()
    entry = itinerary.find_entry(data, entry_id)
    if entry is not None:
        entry["research"] = "done"
        entry["dossier"] = path
        v.write_itinerary(data, f"itinerary: researched {entry.get('place')}")
    for chunk in chunks(f"{text}\n\n(saved to {path})"):
        await context.bot.send_message(chat_id=allowed_user_id(), text=chunk)


async def wishlist_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_authorized(update):
        return
    message = update.message
    assert message is not None
    v = vault()
    text = format_wishlist(v.read_itinerary(), PlaceIndex(v.read_places()))
    for chunk in chunks(text):
        await message.reply_text(chunk)
