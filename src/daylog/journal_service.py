"""The journaling pipeline, free of any client: a transcript in, a LogResult out.

Telegram (bot._log_entry) and the web app (webapp/api.py) both call `log`,
so a note logged on either lands identically. What needs a tap before it's
written (a hard goal or itinerary date, a correction that removes
something, a sport the graph lacks, a fix to a guessed sport) comes back as
plain pending objects; each client renders them its own way (buttons or
cards) and applies the answer with the `confirm_*` functions here.

Order of the pipeline: extract → location → other-day notes → goals →
itinerary → corrections → journal write → discipline guesses.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any

from daylog import brief, disciplines, export, extract, goals, itinerary, vocab
from daylog.disciplines import Graph, Guess
from daylog.places import PlaceIndex
from daylog.vault import Vault, VaultError

logger = logging.getLogger(__name__)


@dataclass
class PendingCorrection:
    entry_date: date
    field: str
    index: int
    item: Any
    description: str
    reason: str | None


@dataclass
class ResolvedOtherDayNote:
    date: date
    facts: dict[str, Any]
    summary: str


@dataclass
class LogResult:
    """What one note did, and what still waits for a confirm."""

    date: date
    summary: str
    facts: dict[str, Any]
    graph: Graph
    # Reply blocks in order (goals, itinerary, location, other days, food,
    # discipline guesses); a block may span several lines.
    blocks: list[str] = field(default_factory=list)
    slips: list[goals.PendingSlip] = field(default_factory=list)
    changes: list[itinerary.PendingChange] = field(default_factory=list)
    corrections: list[PendingCorrection] = field(default_factory=list)
    guesses: list[Guess] = field(default_factory=list)
    new_disciplines: list[disciplines.Proposal] = field(default_factory=list)
    location_changed: bool = False
    place_mentions: bool = False
    # False when the journal file was written but its git commit failed.
    saved: bool = True

    @property
    def notes(self) -> list[str]:
        """The reply blocks as single lines, for clients that list them."""
        return [line for block in self.blocks for line in block.splitlines() if line.strip()]


# -- helpers ------------------------------------------------------------------


def goals_commit_message(
    applied_progress: list[goals.AppliedProgress], applied_slips: list[goals.AppliedSlip]
) -> str:
    parts = [f"{p.goal_id} +{p.delta:g}" for p in applied_progress]
    parts += [f"slip {s.goal_id}" for s in applied_slips]
    return "goals: " + ", ".join(parts)


def goals_reply_note(
    applied_progress: list[goals.AppliedProgress], applied_slips: list[goals.AppliedSlip]
) -> str:
    lines = [f"{p.title}: +{p.delta:g} (total {p.new_progress:g})" for p in applied_progress]
    lines += [f"{s.title}: moved to {s.new_date}" for s in applied_slips]
    return "\n".join(lines)


def itinerary_commit_message(applied: list[itinerary.AppliedChange]) -> str:
    return "itinerary: " + ", ".join(a.place for a in applied)


def itinerary_reply_note(applied: list[itinerary.AppliedChange]) -> str:
    return "\n".join(a.summary for a in applied)


def describe_correction_item(field_name: str, item: Any) -> str:
    if field_name == "activities":
        hours = f", {item['hours']:g}h" if item.get("hours") is not None else ""
        detail = f" — {item['detail']}" if item.get("detail") else ""
        return f"{item.get('type', '?')}{hours}{detail}"
    return str(item)


def resolve_corrections(
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
        field_name = c.get("field")
        index = c.get("index")
        if field_name not in ("activities", "skipped", "open_questions") or index is None:
            continue
        items = (existing_frontmatter or {}).get(field_name) or []
        if not (0 <= index < len(items)):
            continue
        item = items[index]
        resolved.append(
            PendingCorrection(
                entry_date=entry_date,
                field=field_name,
                index=index,
                item=item,
                description=describe_correction_item(field_name, item),
                reason=c.get("reason"),
            )
        )
    return resolved


def resolve_other_day_notes(notes: list[dict[str, Any]]) -> list[ResolvedOtherDayNote]:
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


def has_place_mentions(facts: dict[str, Any]) -> bool:
    return any(
        isinstance(item, dict) and item.get("place_mention") and not item.get("place")
        for field_name in ("activities", "meals")
        for item in facts.get(field_name) or []
    )


_NOTE_HEADING = re.compile(r"^###\s+\d{1,2}:\d{2}", re.MULTILINE)


def note_count(summary: str) -> int:
    """How many notes a day was told in (each starts with a '### HH:MM' heading)."""
    return len(_NOTE_HEADING.findall(summary or ""))


# -- the pipeline ---------------------------------------------------------------


def log(
    v: Vault,
    transcript: str,
    day: date,
    now: datetime,
    *,
    backdated: bool = False,
) -> LogResult:
    """Extract facts from `transcript` and write them under `day`.

    Raises if extraction fails (nothing is written then). A failed journal
    commit is not raised: the entry is on disk, so `saved` is False and
    the caller says so precisely.
    """
    entry_time = datetime.combine(day, now.time())
    entry_date = entry_time.date()

    goals_data = v.read_goals()
    itinerary_data = v.read_itinerary()
    existing_entry = v.read_journal_entry(entry_date)
    location_data = v.read_location()
    current = brief.current_location(location_data)
    places_list = v.read_places()
    graph = Graph(v.read_disciplines())
    facts = extract.extract(
        transcript,
        goals.active_summary(goals_data),
        itinerary.active_summary(itinerary_data),
        entry_date,
        existing_frontmatter=existing_entry.frontmatter if existing_entry else None,
        current_location=current.get("place") if current else None,
        places=places_list,
        current_location_entry=current,
        disciplines=disciplines.outline(graph) if graph else None,
        vocabulary=vocab.custom(v.read_profile()),
        # A backdated note was sent later than the day it's about.
        recorded_at=None if backdated else now.time(),
    )
    summary = facts.pop("summary", "")
    place_index = PlaceIndex(places_list)
    here = place_index.current_place(current)
    guesses, new_disciplines = disciplines.prepare(
        v,
        graph,
        facts,
        entry_date,
        place_index,
        here["id"] if here else None,
        transcript=transcript,
    )
    result = LogResult(
        date=entry_date,
        summary=summary,
        facts=facts,
        graph=graph,
        guesses=guesses,
        new_disciplines=new_disciplines,
    )

    # goal_progress stays in facts — it's part of the journal frontmatter
    # schema too (see docs/DESIGN.md) — but goal_slips, itinerary_changes,
    # corrections, and location_change are bookkeeping for other
    # files/edits, not facts about the day, so none of them belong in
    # the journal file.
    goal_progress = facts.get("goal_progress", [])
    goal_slips = facts.pop("goal_slips", [])
    itinerary_changes = facts.pop("itinerary_changes", [])
    location_change = facts.pop("location_change", None)
    corrections = resolve_corrections(
        existing_entry.frontmatter if existing_entry else None,
        facts.pop("corrections", []),
        entry_date,
    )
    other_day_notes = facts.pop("other_day_notes", [])

    location_note = ""
    if location_change and location_change.get("place"):
        try:
            v.write_location(
                location_change["place"],
                location_change.get("lat"),
                location_change.get("lon"),
                entry_date,
                f"location: {location_change['place']}",
                place_id=location_change.get("place_id"),
                mode=location_change.get("mode"),
            )
            location_note = f"(Updated current location to {location_change['place']})"
            result.location_changed = True
        except VaultError:
            logger.exception("location commit failed")
            location_note = "(location update didn't save — check bot logs)"

    other_day_note = ""
    resolved_notes = resolve_other_day_notes(other_day_notes)
    if resolved_notes:
        applied_other_days = []
        for resolved in resolved_notes:
            other_time = datetime.combine(resolved.date, entry_time.time())
            # Not the raw transcript — the whole recording is mainly
            # about entry_date, and lives there in full. This is a
            # clearly-labeled pointer, not a second verbatim copy.
            stub = f"(Mentioned in the {entry_date.isoformat()} entry) {resolved.summary}"
            try:
                v.write_journal_entry(other_time, resolved.facts, stub, resolved.summary)
                applied_other_days.append(
                    f"Also logged under {resolved.date.isoformat()}: {resolved.summary}"
                )
            except VaultError:
                logger.exception("other-day note commit failed for %s", resolved.date)
                applied_other_days.append(
                    f"(couldn't save the {resolved.date.isoformat()} note — check bot logs)"
                )
        other_day_note = "\n".join(applied_other_days)

    goals_note = ""
    if goal_progress or goal_slips:
        applied_progress = goals.apply_progress(goals_data, goal_progress)
        applied_slips, result.slips = goals.apply_slips(goals_data, goal_slips, on=entry_date)
        if applied_progress or applied_slips:
            try:
                v.write_goals(goals_data, goals_commit_message(applied_progress, applied_slips))
                goals_note = goals_reply_note(applied_progress, applied_slips)
            except VaultError:
                logger.exception("goals commit failed")
                goals_note = "(goal update didn't save — check bot logs)"

    itinerary_note = ""
    if itinerary_changes:
        applied_changes, result.changes = itinerary.apply_itinerary_changes(
            itinerary_data, itinerary_changes, on=entry_date
        )
        if applied_changes:
            try:
                v.write_itinerary(itinerary_data, itinerary_commit_message(applied_changes))
                itinerary_note = itinerary_reply_note(applied_changes)
            except VaultError:
                logger.exception("itinerary commit failed")
                itinerary_note = "(itinerary update didn't save — check bot logs)"

    result.corrections = corrections

    try:
        v.write_journal_entry(entry_time, facts, transcript, summary)
    except VaultError:
        # write_journal_entry writes the file to disk before it commits,
        # so a VaultError here means the entry is sitting on disk,
        # untracked — not lost, just not committed.
        logger.exception("vault commit failed for %s", entry_date)
        result.saved = False
        return result

    food_note = ""
    if facts.get("meals"):
        logged = v.read_journal_entry(entry_date)
        food_line = export.day_summary(logged.frontmatter) if logged else None
        if food_line:
            food_note = f"{food_line} so far that day"

    guess_note = disciplines.guess_note(graph, guesses)
    result.blocks = [
        b
        for b in (goals_note, itinerary_note, location_note, other_day_note, food_note, guess_note)
        if b
    ]
    result.place_mentions = has_place_mentions(facts)
    return result


# -- asking, and applying a confirm ---------------------------------------------------


def slip_question(slip: goals.PendingSlip) -> str:
    old = slip.old_date or "unset"
    reason = f" ({slip.reason})" if slip.reason else ""
    return f"'{slip.title}' is a hard deadline — move it from {old} to {slip.new_date}?{reason}"


def change_question(change: itinerary.PendingChange) -> str:
    old = change.old_date or "unset"
    new_note = " (new entry)" if change.is_new else ""
    reason = f" ({change.reason})" if change.reason else ""
    return (
        f"'{change.place}'{new_note} is a hard date — set it from {old} to "
        f"{change.new_date}?{reason}"
    )


def correction_question(correction: PendingCorrection) -> str:
    reason = f" ({correction.reason})" if correction.reason else ""
    return (
        f"Remove from {correction.entry_date.isoformat()}'s {correction.field}: "
        f"'{correction.description}'?{reason}"
    )


def confirm_slip(v: Vault, slip: goals.PendingSlip, on: date) -> None:
    """Move a hard goal's date after the user confirmed it. Raises VaultError."""
    goals_data = v.read_goals()
    goals.apply_confirmed_slip(goals_data, slip, on=on)
    v.write_goals(goals_data, f"goals: slip {slip.goal_id}")


def confirm_change(v: Vault, change: itinerary.PendingChange, on: date) -> None:
    """Set a hard itinerary date after the user confirmed it. Raises VaultError."""
    itinerary_data = v.read_itinerary()
    itinerary.apply_confirmed_change(itinerary_data, change, on=on)
    v.write_itinerary(itinerary_data, f"itinerary: {change.place}")


def confirm_correction(v: Vault, correction: PendingCorrection) -> None:
    """Remove the item a correction named. Raises CorrectionConflictError when the
    entry changed since, VaultError when the commit fails."""
    v.remove_journal_item(
        correction.entry_date,
        correction.field,
        correction.index,
        correction.item,
        f"journal: {correction.entry_date.isoformat()} (correction)",
    )
