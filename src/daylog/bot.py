"""Telegram handlers and entrypoint.

Every handler checks the sender against TELEGRAM_ALLOWED_USER_ID before
doing anything else — this is the only auth layer, so it must run first.
"""

from __future__ import annotations

import logging
import os
import tempfile
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from datetime import time as dt_time
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from dotenv import load_dotenv
from telegram import BotCommand, InlineKeyboardButton, InlineKeyboardMarkup, Message, Update
from telegram.ext import (
    Application,
    ApplicationBuilder,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

from daylog import (
    brief,
    calendar_server,
    chat_flow,
    daily,
    extract,
    goals,
    itinerary,
    llm,
    plan_flow,
    rank_flow,
    rankings,
    research_flow,
    trail,
    transcribe,
    wishlist_flow,
)
from daylog.dateparse import parse_date_phrase
from daylog.goals import active_summary as _active_goals_summary
from daylog.itinerary import active_summary as _active_itinerary_summary
from daylog.places import PlaceIndex
from daylog.sources import marine, wind
from daylog.tg import allowed_user_id as _allowed_user_id
from daylog.tg import chunks as _chunks
from daylog.tg import is_authorized as _is_authorized
from daylog.tg import tz as _tz
from daylog.tg import vault as _vault
from daylog.vault import CorrectionConflictError, VaultError

logger = logging.getLogger(__name__)

_PENDING_DATE_KEY = "pending_entry_date"
_PENDING_SLIP_KEY = "pending_goal_slips"
_PENDING_ITIN_KEY = "pending_itinerary_changes"
_PENDING_CORRECTION_KEY = "pending_corrections"


def _goals_commit_message(
    applied_progress: list[goals.AppliedProgress], applied_slips: list[goals.AppliedSlip]
) -> str:
    parts = [f"{p.goal_id} +{p.delta:g}" for p in applied_progress]
    parts += [f"slip {s.goal_id}" for s in applied_slips]
    return "goals: " + ", ".join(parts)


def _goals_reply_note(
    applied_progress: list[goals.AppliedProgress], applied_slips: list[goals.AppliedSlip]
) -> str:
    lines = [f"{p.title}: +{p.delta:g} (total {p.new_progress:g})" for p in applied_progress]
    lines += [f"{s.title}: moved to {s.new_date}" for s in applied_slips]
    return "\n".join(lines)


def _itinerary_commit_message(applied: list[itinerary.AppliedChange]) -> str:
    return "itinerary: " + ", ".join(a.place for a in applied)


def _itinerary_reply_note(applied: list[itinerary.AppliedChange]) -> str:
    return "\n".join(a.summary for a in applied)


def _format_goal_line(g: Any) -> str:
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


def _format_itinerary_line(e: Any) -> str:
    status = e.get("status", "candidate")
    line = f"- {e.get('place', e['id'])} [{status}] ({e.get('type', 'soft')})"
    entry_date = itinerary.current_date(e)
    if entry_date:
        label = "deadline" if e.get("type") == "hard" else "target"
        line += f": {label} {entry_date}"
    return line


@dataclass
class PendingCorrection:
    entry_date: date
    field: str
    index: int
    item: Any
    description: str
    reason: str | None


def _describe_correction_item(field: str, item: Any) -> str:
    if field == "activities":
        hours = f", {item['hours']:g}h" if item.get("hours") is not None else ""
        detail = f" — {item['detail']}" if item.get("detail") else ""
        return f"{item.get('type', '?')}{hours}{detail}"
    return str(item)


def _resolve_corrections(
    existing_frontmatter: dict[str, Any] | None,
    corrections: list[dict[str, Any]],
    entry_date: date,
) -> list[PendingCorrection]:
    """Validate extracted correction references against what's actually on
    the entry, dropping anything stale or out of range rather than trusting
    the model's index blindly.
    """
    resolved = []
    for c in corrections:
        field = c.get("field")
        index = c.get("index")
        if field not in ("activities", "skipped", "open_questions") or index is None:
            continue
        items = (existing_frontmatter or {}).get(field) or []
        if not (0 <= index < len(items)):
            continue
        item = items[index]
        resolved.append(
            PendingCorrection(
                entry_date=entry_date,
                field=field,
                index=index,
                item=item,
                description=_describe_correction_item(field, item),
                reason=c.get("reason"),
            )
        )
    return resolved


@dataclass
class ResolvedOtherDayNote:
    date: date
    facts: dict[str, Any]
    summary: str


def _resolve_other_day_notes(notes: list[dict[str, Any]]) -> list[ResolvedOtherDayNote]:
    """Validate/parse raw other_day_notes extraction output, dropping anything malformed
    (missing date/summary, or a date string that isn't real) rather than trusting it blindly.
    """
    resolved = []
    for note in notes:
        date_str = note.get("date")
        summary = note.get("summary")
        if not date_str or not summary:
            continue
        try:
            note_date = date.fromisoformat(date_str)
        except ValueError:
            continue
        facts = {key: note[key] for key in ("activities", "skipped", "mood") if note.get(key)}
        resolved.append(ResolvedOtherDayNote(date=note_date, facts=facts, summary=summary))
    return resolved


def _format_status(goals_data: Any, itinerary_data: Any) -> str:
    goal_lines = [_format_goal_line(g) for g in goals_data if g.get("status") != "dropped"]
    itin_lines = [_format_itinerary_line(e) for e in itinerary_data if e.get("status") != "dropped"]
    return (
        "Goals:\n"
        + ("\n".join(goal_lines) if goal_lines else "(none)")
        + "\n\nItinerary:\n"
        + ("\n".join(itin_lines) if itin_lines else "(none)")
    )


async def status(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _is_authorized(update):
        return
    message = update.message
    assert message is not None
    vault = _vault()
    await message.reply_text(_format_status(vault.read_goals(), vault.read_itinerary()))


def _upcoming_items(goals_data: Any, itinerary_data: Any, today: date) -> list[str]:
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


async def upcoming(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _is_authorized(update):
        return
    message = update.message
    assert message is not None
    vault = _vault()
    today = datetime.now(_tz()).date()
    lines = _upcoming_items(vault.read_goals(), vault.read_itinerary(), today)
    await message.reply_text("\n".join(lines) if lines else "Nothing dated yet.")


def _trail_range(args: list[str], today: date, location_data: Any) -> tuple[date, date]:
    """/trail -> last 30 days; /trail 60 -> last 60; /trail all -> since the first stay."""
    if args and args[0].lower() == "all":
        starts = [s.start for s in trail.stays(location_data)]
        return (min(starts) if starts else today - timedelta(days=30)), today
    days = int(args[0]) if args and args[0].isdigit() else 30
    return today - timedelta(days=days - 1), today


async def trail_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _is_authorized(update):
        return
    message = update.message
    assert message is not None
    vault = _vault()
    today = datetime.now(_tz()).date()
    location_data = vault.read_location()
    start, end = _trail_range(list(context.args or []), today, location_data)
    entries = vault.read_journal_range(start, end)
    days = trail.build(
        location_data,
        {d: e.frontmatter for d, e in entries.items()},
        {d: e.summary for d, e in entries.items()},
        start,
        end,
    )
    text = trail.format_trail(days, PlaceIndex(vault.read_places()))
    for chunk in _chunks(text):
        await message.reply_text(chunk)


def _format_place_card(
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


async def place_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _is_authorized(update):
        return
    message = update.message
    assert message is not None
    query = " ".join(context.args or []).strip()
    if not query:
        await message.reply_text("Usage: /place <name>, e.g. /place Lakey Peak")
        return
    vault = _vault()
    index = PlaceIndex(vault.read_places())
    matches = index.resolve(query)
    if not matches:
        needle = query.lower()
        matches = [i for i, n in index.by_id.items() if needle in str(n.get("name", "")).lower()]
    if not matches:
        await message.reply_text(f"No place matching '{query}' yet.")
        return
    today = datetime.now(_tz()).date()
    entries = vault.read_journal_range(date(2000, 1, 1), today)
    visited = trail.visits({d: e.frontmatter for d, e in entries.items()}, index)
    node = index.by_id[matches[0]]
    category = rankings.category_for(node.get("kind"))
    rank_line = (
        rankings.describe(vault.read_yaml(rank_flow.RANKINGS_FILE, {}), category, node["id"])
        if category
        else None
    )
    card = _format_place_card(index, node, visited.get(node["id"], []), rank_line)
    if len(matches) > 1:
        others = ", ".join(index.path_name(m) for m in matches[1:5])
        card += f"\n\nAlso matching: {others}"
    for chunk in _chunks(card):
        await message.reply_text(chunk)


def _brief_hour() -> int:
    return int(os.environ.get("BRIEF_HOUR", "7"))


def _relevant_spots(current: dict[str, Any] | None, places_data: Any) -> list[dict[str, Any]]:
    """Current location plus every surf/wind spot with coordinates in its region.

    Comparing multiple nearby spots (not just where the user happens to be
    standing) is the point — swell or wind can be building somewhere better
    a short trip away.
    """
    if not current or current.get("lat") is None or current.get("lon") is None:
        return []

    spots = [
        {
            "name": current.get("place", "current location"),
            "lat": current["lat"],
            "lon": current["lon"],
        }
    ]
    index = PlaceIndex(list(places_data or []))
    for spot in index.forecast_spots(index.current_place(current)):
        spots.append(
            {"name": spot.get("name", "nearby spot"), "lat": spot["lat"], "lon": spot["lon"]}
        )
    return spots


def _fetch_conditions(
    current: dict[str, Any] | None,
    places_data: Any,
    tz: ZoneInfo,
    fetch_fn: Callable[[float, float, str], str | None],
) -> str | None:
    blocks = []
    for spot in _relevant_spots(current, places_data):
        forecast = fetch_fn(spot["lat"], spot["lon"], str(tz))
        if forecast:
            blocks.append(f"{spot['name']}:\n{forecast}")
    return "\n\n".join(blocks) if blocks else None


def _fetch_marine_forecast(
    current: dict[str, Any] | None, places_data: Any, tz: ZoneInfo
) -> str | None:
    return _fetch_conditions(current, places_data, tz, marine.fetch_forecast)


def _fetch_wind_forecast(
    current: dict[str, Any] | None, places_data: Any, tz: ZoneInfo
) -> str | None:
    return _fetch_conditions(current, places_data, tz, wind.fetch_forecast)


async def _send_brief(context: ContextTypes.DEFAULT_TYPE, chat_id: int) -> None:
    bot = context.bot
    await bot.send_message(chat_id=chat_id, text="Building your brief...")
    try:
        vault = _vault()
        today = datetime.now(_tz()).date()
        location_data = vault.read_location()
        places_data = vault.read_places()

        current = brief.current_location(location_data)
        marine_forecast = _fetch_marine_forecast(current, places_data, _tz())
        wind_forecast = _fetch_wind_forecast(current, places_data, _tz())

        text = brief.generate_brief(
            today=today,
            goals_data=vault.read_goals(),
            itinerary_data=vault.read_itinerary(),
            places_data=places_data,
            location_data=location_data,
            profile_data=vault.read_profile(),
            recent_journal=brief.recent_journal_summaries(vault, today),
            marine_forecast=marine_forecast,
            wind_forecast=wind_forecast,
            dossier_digests={
                name: wishlist_flow.dossier_digest(text)
                for name, text in wishlist_flow.dossiers_for(vault, vault.read_itinerary()).items()
            },
            morning_research=vault.read_text(f"research/daily/{today.isoformat()}.md"),
            current_plan=plan_flow.latest_plan(vault),
        )
    except Exception:
        logger.exception("failed to generate brief")
        await bot.send_message(
            chat_id=chat_id, text="Something went wrong building your brief — check bot logs."
        )
        return

    for chunk in _chunks(text):
        await bot.send_message(chat_id=chat_id, text=chunk)
    chat_flow.remember(context, "The bot sent the morning brief", text)
    llm.flush(vault)


async def brief_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _is_authorized(update):
        return
    message = update.message
    assert message is not None
    await _send_brief(context, message.chat_id)


async def send_scheduled_brief(context: ContextTypes.DEFAULT_TYPE) -> None:
    await _send_brief(context, _allowed_user_id())


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _is_authorized(update):
        return
    message = update.message
    assert message is not None
    await message.reply_text(
        "daylog is listening.\n\n"
        "🎙 A voice note logs today (before 4am it still counts as yesterday).\n"
        "📝 Yesterday / 📅 Pick date: your next voice note or message logs to that day.\n"
        "✍️ Type an entry: your next typed message is today's journal.\n"
        "💬 Anything else you type goes to the assistant — ask about your trips, "
        "plans, places, rankings, or where to go next.\n\n"
        'Corrections work by voice too ("actually I only surfed 1 hour") — I\'ll ask '
        "before removing anything.",
        reply_markup=chat_flow.MENU,
    )


def _format_short_date(d: date) -> str:
    return f"{d.strftime('%b')} {d.day}"


def _backdate_options(today: date, days: int = 7) -> list[tuple[date, str]]:
    """(date, label) pairs for the /backdate picker, today first then going back."""
    options = []
    for offset in range(days):
        d = today - timedelta(days=offset)
        if offset == 0:
            name = "Today"
        elif offset == 1:
            name = "Yesterday"
        else:
            name = d.strftime("%A")
        options.append((d, f"{name} ({_format_short_date(d)})"))
    return options


async def backdate_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _is_authorized(update):
        return
    message = update.message
    assert message is not None
    await _send_backdate_picker(message, daily.logical_day(datetime.now(_tz())))


async def _send_backdate_picker(message: Message, today: date) -> None:
    keyboard = InlineKeyboardMarkup(
        [
            [InlineKeyboardButton(label, callback_data=f"backdate:{d.isoformat()}")]
            for d, label in _backdate_options(today)
        ]
    )
    await message.reply_text("Log your next message under which day?", reply_markup=keyboard)


async def handle_backdate_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _is_authorized(update):
        return
    query = update.callback_query
    assert query is not None and query.data is not None
    await query.answer()

    _, iso_date = query.data.split(":", 1)
    picked = date.fromisoformat(iso_date)

    assert context.user_data is not None
    context.user_data[_PENDING_DATE_KEY] = picked

    await query.edit_message_text(
        f"Got it — your next message will be logged as {picked.isoformat()}."
    )


async def _ask_hard_slip_confirmation(
    message: Message, context: ContextTypes.DEFAULT_TYPE, slip: goals.PendingSlip
) -> None:
    assert context.user_data is not None
    confirm_id = uuid.uuid4().hex
    context.user_data.setdefault(_PENDING_SLIP_KEY, {})[confirm_id] = slip

    old = slip.old_date or "unset"
    reason = f" ({slip.reason})" if slip.reason else ""
    keyboard = InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("Confirm", callback_data=f"goalslip:confirm:{confirm_id}"),
                InlineKeyboardButton("Cancel", callback_data=f"goalslip:cancel:{confirm_id}"),
            ]
        ]
    )
    await message.reply_text(
        f"'{slip.title}' is a hard deadline — move it from {old} to {slip.new_date}?{reason}",
        reply_markup=keyboard,
    )


async def handle_goal_slip_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _is_authorized(update):
        return
    query = update.callback_query
    assert query is not None and query.data is not None
    await query.answer()

    _, action, confirm_id = query.data.split(":", 2)

    assert context.user_data is not None
    pending: dict[str, goals.PendingSlip] = context.user_data.get(_PENDING_SLIP_KEY, {})
    slip = pending.pop(confirm_id, None)
    if slip is None:
        await query.edit_message_text("This confirmation has expired or was already handled.")
        return

    if action == "cancel":
        await query.edit_message_text(f"Cancelled — '{slip.title}' deadline unchanged.")
        return

    vault = _vault()
    goals_data = vault.read_goals()
    goals.apply_confirmed_slip(goals_data, slip, on=datetime.now(_tz()).date())
    try:
        vault.write_goals(goals_data, f"goals: slip {slip.goal_id}")
    except VaultError:
        logger.exception("goals commit failed confirming slip for %s", slip.goal_id)
        await query.edit_message_text(
            f"Confirmed, but the git commit failed for '{slip.title}' — check bot logs."
        )
        return

    await query.edit_message_text(f"Confirmed — '{slip.title}' deadline moved to {slip.new_date}.")


async def _ask_hard_itinerary_confirmation(
    message: Message, context: ContextTypes.DEFAULT_TYPE, change: itinerary.PendingChange
) -> None:
    assert context.user_data is not None
    confirm_id = uuid.uuid4().hex
    context.user_data.setdefault(_PENDING_ITIN_KEY, {})[confirm_id] = change

    old = change.old_date or "unset"
    new_note = " (new entry)" if change.is_new else ""
    reason = f" ({change.reason})" if change.reason else ""
    keyboard = InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("Confirm", callback_data=f"itin:confirm:{confirm_id}"),
                InlineKeyboardButton("Cancel", callback_data=f"itin:cancel:{confirm_id}"),
            ]
        ]
    )
    await message.reply_text(
        f"'{change.place}'{new_note} is a hard date — set it from {old} to "
        f"{change.new_date}?{reason}",
        reply_markup=keyboard,
    )


async def handle_itinerary_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _is_authorized(update):
        return
    query = update.callback_query
    assert query is not None and query.data is not None
    await query.answer()

    _, action, confirm_id = query.data.split(":", 2)

    assert context.user_data is not None
    pending: dict[str, itinerary.PendingChange] = context.user_data.get(_PENDING_ITIN_KEY, {})
    change = pending.pop(confirm_id, None)
    if change is None:
        await query.edit_message_text("This confirmation has expired or was already handled.")
        return

    if action == "cancel":
        await query.edit_message_text(f"Cancelled — '{change.place}' left unchanged.")
        return

    vault = _vault()
    itinerary_data = vault.read_itinerary()
    itinerary.apply_confirmed_change(itinerary_data, change, on=datetime.now(_tz()).date())
    try:
        vault.write_itinerary(itinerary_data, f"itinerary: {change.place}")
    except VaultError:
        logger.exception("itinerary commit failed confirming change for %s", change.id)
        await query.edit_message_text(
            f"Confirmed, but the git commit failed for '{change.place}' — check bot logs."
        )
        return

    await query.edit_message_text(f"Confirmed — '{change.place}' set to {change.new_date}.")


async def _ask_correction_confirmation(
    message: Message, context: ContextTypes.DEFAULT_TYPE, correction: PendingCorrection
) -> None:
    assert context.user_data is not None
    confirm_id = uuid.uuid4().hex
    context.user_data.setdefault(_PENDING_CORRECTION_KEY, {})[confirm_id] = correction

    reason = f" ({correction.reason})" if correction.reason else ""
    keyboard = InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("Confirm", callback_data=f"correction:confirm:{confirm_id}"),
                InlineKeyboardButton("Cancel", callback_data=f"correction:cancel:{confirm_id}"),
            ]
        ]
    )
    await message.reply_text(
        f"Remove from {correction.entry_date.isoformat()}'s {correction.field}: "
        f"'{correction.description}'?{reason}",
        reply_markup=keyboard,
    )


async def handle_correction_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _is_authorized(update):
        return
    query = update.callback_query
    assert query is not None and query.data is not None
    await query.answer()

    _, action, confirm_id = query.data.split(":", 2)

    assert context.user_data is not None
    pending: dict[str, PendingCorrection] = context.user_data.get(_PENDING_CORRECTION_KEY, {})
    correction = pending.pop(confirm_id, None)
    if correction is None:
        await query.edit_message_text("This confirmation has expired or was already handled.")
        return

    if action == "cancel":
        await query.edit_message_text(f"Cancelled — '{correction.description}' left unchanged.")
        return

    vault = _vault()
    try:
        vault.remove_journal_item(
            correction.entry_date,
            correction.field,
            correction.index,
            correction.item,
            f"journal: {correction.entry_date.isoformat()} (correction)",
        )
    except CorrectionConflictError:
        await query.edit_message_text(
            f"'{correction.description}' no longer matches today's entry — it may have "
            "already changed. Nothing was removed; say the correction again if it's still needed."
        )
        return
    except VaultError:
        logger.exception("correction commit failed for %s", correction.entry_date)
        await query.edit_message_text(
            f"Confirmed, but the git commit failed removing '{correction.description}' — "
            "check bot logs."
        )
        return

    await query.edit_message_text(
        f"Removed '{correction.description}' from {correction.entry_date.isoformat()}."
    )


async def _log_entry(transcript: str, message: Message, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Shared pipeline: transcript text -> extract -> apply goals -> vault write -> reply.

    Used by both the voice and text handlers once each has produced a
    transcript string — voice via faster-whisper, text directly from the
    Telegram message.
    """
    try:
        await message.reply_text("Extracting...")

        pending_date = context.user_data.pop(_PENDING_DATE_KEY, None) if context.user_data else None
        now = datetime.now(_tz())
        # Before the day cutoff (4am by default) a note still belongs to the
        # day that's ending — see daily.logical_day.
        day = pending_date or daily.logical_day(now)
        entry_time = datetime.combine(day, now.time())

        vault = _vault()
        goals_data = vault.read_goals()
        itinerary_data = vault.read_itinerary()
        existing_entry = vault.read_journal_entry(entry_time.date())
        location_data = vault.read_location()
        current = brief.current_location(location_data)
        facts = extract.extract(
            transcript,
            _active_goals_summary(goals_data),
            _active_itinerary_summary(itinerary_data),
            entry_time.date(),
            existing_frontmatter=existing_entry.frontmatter if existing_entry else None,
            current_location=current.get("place") if current else None,
            places=vault.read_places(),
            current_location_entry=current,
        )
        summary = facts.pop("summary", "")

        # goal_progress stays in facts — it's part of the journal frontmatter
        # schema too (SPEC §5.1) — but goal_slips, itinerary_changes,
        # corrections, and location_change are bookkeeping for other
        # files/edits, not facts about the day, so none of them belong in
        # the journal file.
        goal_progress = facts.get("goal_progress", [])
        goal_slips = facts.pop("goal_slips", [])
        itinerary_changes = facts.pop("itinerary_changes", [])
        location_change = facts.pop("location_change", None)
        corrections = _resolve_corrections(
            existing_entry.frontmatter if existing_entry else None,
            facts.pop("corrections", []),
            entry_time.date(),
        )
        other_day_notes = facts.pop("other_day_notes", [])

        location_note = ""
        if location_change and location_change.get("place"):
            try:
                vault.write_location(
                    location_change["place"],
                    location_change.get("lat"),
                    location_change.get("lon"),
                    entry_time.date(),
                    f"location: {location_change['place']}",
                    place_id=location_change.get("place_id"),
                    mode=location_change.get("mode"),
                )
                location_note = f"\n\n(Updated current location to {location_change['place']})"
                context.application.create_task(research_flow.arrive(context, message.chat_id))
            except VaultError:
                logger.exception("location commit failed")
                location_note = "\n\n(location update didn't save — check bot logs)"

        other_day_note = ""
        resolved_notes = _resolve_other_day_notes(other_day_notes)
        if resolved_notes:
            applied_other_days = []
            for resolved in resolved_notes:
                other_time = datetime.combine(resolved.date, entry_time.time())
                # Not the raw transcript — the whole recording is mainly
                # about entry_time.date(), and lives there in full. This
                # is a clearly-labeled pointer, not a second verbatim copy.
                stub = (
                    f"(Mentioned in the {entry_time.date().isoformat()} entry) {resolved.summary}"
                )
                try:
                    vault.write_journal_entry(other_time, resolved.facts, stub, resolved.summary)
                    applied_other_days.append(
                        f"Also logged under {resolved.date.isoformat()}: {resolved.summary}"
                    )
                except VaultError:
                    logger.exception("other-day note commit failed for %s", resolved.date)
                    applied_other_days.append(
                        f"(couldn't save the {resolved.date.isoformat()} note — check bot logs)"
                    )
            if applied_other_days:
                other_day_note = "\n\n" + "\n".join(applied_other_days)

        goals_note = ""
        if goal_progress or goal_slips:
            applied_progress = goals.apply_progress(goals_data, goal_progress)
            applied_slips, pending_slips = goals.apply_slips(
                goals_data, goal_slips, on=entry_time.date()
            )

            if applied_progress or applied_slips:
                try:
                    vault.write_goals(
                        goals_data, _goals_commit_message(applied_progress, applied_slips)
                    )
                    goals_note = "\n\n" + _goals_reply_note(applied_progress, applied_slips)
                except VaultError:
                    logger.exception("goals commit failed")
                    goals_note = "\n\n(goal update didn't save — check bot logs)"

            for slip in pending_slips:
                await _ask_hard_slip_confirmation(message, context, slip)

        itinerary_note = ""
        if itinerary_changes:
            applied_changes, pending_changes = itinerary.apply_itinerary_changes(
                itinerary_data, itinerary_changes, on=entry_time.date()
            )

            if applied_changes:
                try:
                    vault.write_itinerary(
                        itinerary_data, _itinerary_commit_message(applied_changes)
                    )
                    itinerary_note = "\n\n" + _itinerary_reply_note(applied_changes)
                except VaultError:
                    logger.exception("itinerary commit failed")
                    itinerary_note = "\n\n(itinerary update didn't save — check bot logs)"

            for change in pending_changes:
                await _ask_hard_itinerary_confirmation(message, context, change)

        for correction in corrections:
            await _ask_correction_confirmation(message, context, correction)

        try:
            vault.write_journal_entry(entry_time, facts, transcript, summary)
        except VaultError:
            # write_journal_entry writes the file to disk before it commits,
            # so a VaultError here means the entry is sitting on disk,
            # untracked — not lost, just not committed. Say so precisely,
            # since "nothing was saved" would be wrong and send the user
            # looking for a bug that isn't there.
            logger.exception("vault commit failed for %s", entry_time.date())
            await message.reply_text(
                f"Transcribed {entry_time.date().isoformat()} and wrote it to the vault, but "
                "the git commit failed — it's on disk, just not committed yet. Check the "
                "bot's logs (likely a git config issue) and it'll get picked up next time "
                "you log."
            )
            return

        await message.reply_text(
            f"Logged {entry_time.date().isoformat()}:\n\n{summary}"
            f"{goals_note}{itinerary_note}{location_note}{other_day_note}",
            reply_markup=chat_flow.MENU,
        )
        try:
            await rank_flow.prompt_after_entry(message, context, facts)
        except Exception:
            logger.exception("ranking prompt failed")
        if _has_place_mentions(facts):
            context.application.create_task(
                research_flow.resolve_days(context, message.chat_id, [entry_time.date()])
            )
    except Exception:
        logger.exception("failed to process entry")
        await message.reply_text(
            "Something went wrong logging that. Nothing was saved — try again?"
        )


def _has_place_mentions(facts: dict[str, Any]) -> bool:
    return any(
        isinstance(item, dict) and item.get("place_mention") and not item.get("place")
        for field_name in ("activities", "meals")
        for item in facts.get(field_name) or []
    )


async def handle_voice(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _is_authorized(update):
        return
    message = update.message
    assert message is not None and message.voice is not None

    try:
        with tempfile.TemporaryDirectory() as tmp_dir:
            ogg_path = Path(tmp_dir) / "voice.ogg"
            telegram_file = await message.voice.get_file()
            await telegram_file.download_to_drive(custom_path=ogg_path)

            await message.reply_text("Transcribing...")
            transcript = transcribe.transcribe(ogg_path)
    except Exception:
        logger.exception("failed to transcribe voice note")
        await message.reply_text(
            "Something went wrong transcribing that. Nothing was saved — try again?"
        )
        return

    if not transcript:
        await message.reply_text("Couldn't make out any speech in that voice note.")
        return

    await _log_entry(transcript, message, context)


async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Menu buttons, then journal (when a day was picked), else the assistant."""
    if not _is_authorized(update):
        return
    message = update.message
    assert message is not None and message.text is not None
    text = message.text.strip()
    assert context.user_data is not None
    now = datetime.now(_tz())
    today = daily.logical_day(now)

    if text == chat_flow.BTN_YESTERDAY:
        context.user_data[_PENDING_DATE_KEY] = today - timedelta(days=1)
        await message.reply_text(
            f"Logging to yesterday ({_format_short_date(today - timedelta(days=1))}) — "
            "send a voice note or type it."
        )
        return
    if text == chat_flow.BTN_PICK:
        await _send_backdate_picker(message, today)
        return
    if text == chat_flow.BTN_TYPE:
        context.user_data[_PENDING_DATE_KEY] = today
        await message.reply_text(f"Type today's entry ({_format_short_date(today)}).")
        return

    if context.user_data.get(_PENDING_DATE_KEY) is not None:
        await _log_entry(text, message, context)
        return

    override_date = parse_date_phrase(text, today=today)
    if override_date is not None:
        context.user_data[_PENDING_DATE_KEY] = override_date
        await message.reply_text(
            f"Got it — your next message will be logged as {override_date.isoformat()}."
        )
        return

    await chat_flow.handle_chat(message, context, text)


async def _post_init(application: Application) -> None:  # type: ignore[type-arg]
    # Registers Telegram's native "/" command menu, so the available
    # commands are tappable instead of something to remember/type exactly.
    await application.bot.set_my_commands(
        [
            BotCommand("start", "How to use daylog"),
            BotCommand("status", "Show current goals and itinerary"),
            BotCommand("upcoming", "Show dated goals/itinerary, earliest first"),
            BotCommand("brief", "Get a daily brief now"),
            BotCommand("backdate", "Log your next message under a recent past date"),
            BotCommand("trail", "Where you've been (/trail, /trail 60, /trail all)"),
            BotCommand("place", "What's known about a place (/place Lakey Peak)"),
            BotCommand("rank", "Rank or re-rank a place (/rank Artisan)"),
            BotCommand("reconcile", "Rebuild a day from all its notes now"),
            BotCommand("explore", "Research the spots around where you are"),
            BotCommand("research", "Write a research file on a place (/research Mentawai)"),
            BotCommand("trip", "Reconstruct the stops of your last multi-day trip"),
            BotCommand("backfill", "Link unlinked place names in recent entries"),
            BotCommand("usage", "API spend this month"),
            BotCommand("want", "Add a destination to your wishlist (/want Mentawai)"),
            BotCommand("wishlist", "Destinations you want to go, with research status"),
            BotCommand("plan", "Plan the next few weeks (2-3 options to pick from)"),
            BotCommand("review", "This week's review now"),
            BotCommand("undo", "Revert one of the bot's recent changes"),
            BotCommand("rankings", "Your rankings (/rankings food)"),
        ]
    )


def build_application() -> Application:  # type: ignore[type-arg]
    token = os.environ["TELEGRAM_BOT_TOKEN"]
    application = ApplicationBuilder().token(token).post_init(_post_init).build()
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("status", status))
    application.add_handler(CommandHandler("upcoming", upcoming))
    application.add_handler(CommandHandler("brief", brief_command))
    application.add_handler(CommandHandler("backdate", backdate_command))
    application.add_handler(CommandHandler("trail", trail_command))
    application.add_handler(CommandHandler("place", place_command))
    application.add_handler(CommandHandler("rank", rank_flow.rank_command))
    application.add_handler(CommandHandler("reconcile", daily.reconcile_command))
    application.add_handler(CommandHandler("explore", research_flow.explore_command))
    application.add_handler(CommandHandler("research", research_flow.research_command))
    application.add_handler(CommandHandler("trip", research_flow.trip_command))
    application.add_handler(CommandHandler("backfill", research_flow.backfill_command))
    application.add_handler(CommandHandler("usage", research_flow.usage_command))
    application.add_handler(CommandHandler("want", wishlist_flow.want_command))
    application.add_handler(CommandHandler("wishlist", wishlist_flow.wishlist_command))
    application.add_handler(
        CallbackQueryHandler(wishlist_flow.handle_want_callback, pattern=r"^want:")
    )
    application.add_handler(
        CallbackQueryHandler(chat_flow.handle_proposal_callback, pattern=r"^chatitin:")
    )
    application.add_handler(CommandHandler("plan", plan_flow.plan_command))
    application.add_handler(CommandHandler("review", daily.review_command))
    application.add_handler(CommandHandler("undo", chat_flow.undo_command))
    application.add_handler(CallbackQueryHandler(chat_flow.handle_undo_callback, pattern=r"^undo:"))
    application.add_handler(CallbackQueryHandler(plan_flow.handle_plan_callback, pattern=r"^plan:"))
    application.add_handler(
        CallbackQueryHandler(research_flow.handle_place_callback, pattern=r"^place:")
    )
    application.add_handler(CommandHandler("rankings", rank_flow.rankings_command))
    application.add_handler(CallbackQueryHandler(rank_flow.handle_rank_callback, pattern=r"^rank:"))
    application.add_handler(MessageHandler(filters.VOICE, handle_voice))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))
    application.add_handler(CallbackQueryHandler(handle_goal_slip_callback, pattern=r"^goalslip:"))
    application.add_handler(CallbackQueryHandler(handle_itinerary_callback, pattern=r"^itin:"))
    application.add_handler(
        CallbackQueryHandler(handle_correction_callback, pattern=r"^correction:")
    )
    application.add_handler(CallbackQueryHandler(handle_backdate_callback, pattern=r"^backdate:"))

    assert application.job_queue is not None
    application.job_queue.run_daily(
        send_scheduled_brief, time=dt_time(hour=_brief_hour(), tzinfo=_tz())
    )
    application.job_queue.run_daily(
        daily.reconcile_job, time=dt_time(hour=daily.day_cutoff_hour(), minute=5, tzinfo=_tz())
    )
    # PTB counts days from Sunday = 0.
    application.job_queue.run_daily(
        daily.weekly_review_job,
        time=dt_time(hour=daily.review_hour(), tzinfo=_tz()),
        days=(0,),
    )
    return application


def main() -> None:
    load_dotenv()
    logging.basicConfig(level=logging.INFO)
    # httpx (and the libraries built on it) log every request at INFO,
    # which drowns daylog's own logs under the constant getUpdates polling.
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
    calendar_server.start()
    application = build_application()
    application.run_polling()


if __name__ == "__main__":
    main()
