"""Applying the assistant's proposed changes once the user confirms them.

The assistant (chat.py) only proposes; each proposal becomes a confirm card
in the web app (webapp/api.py), and a Confirm tap calls apply_edit.
"""

from __future__ import annotations

from datetime import date
from typing import Any

from daylog import chat, edits, itinerary, rank_flow
from daylog.vault import Vault


def apply_edit(v: Vault, proposal: dict[str, Any], today: date) -> str:
    """Apply one confirmed chat proposal to the vault. Returns what changed."""
    # "edit" names the kind of change; "kind" inside a place edit is the
    # place's own kind (surf_spot, food, ...), so the two must not share a key.
    kind = proposal.get("edit", "itinerary")
    if kind == "itinerary":
        data = v.read_itinerary()
        outcome = apply_proposal(data, proposal, today)
        v.write_itinerary(data, f"itinerary: {proposal.get('place') or proposal.get('id')}")
        return outcome
    if kind == "place":
        files = v.read_place_files()
        changed, outcome = edits.edit_place(files, proposal)
        v.write_place_files(files, changed, f"places: {outcome}")
        return outcome
    if kind == "delete_place":
        files = v.read_place_files()
        place_id = str(proposal.get("place_id"))
        changed, outcome = edits.delete_place(files, place_id, chat.linked_days(v, place_id, today))
        v.write_place_files(files, changed, f"places: {outcome}")
        return outcome
    if kind == "merge":
        source, target = str(proposal.get("source_id")), str(proposal.get("target_id"))
        days = chat.linked_days(v, source, today)
        files = v.read_place_files()
        changed, outcome = edits.merge_places(files, source, target)
        v.write_place_files(files, changed, f"places: {outcome}")
        for day in days:
            entry = v.read_journal_entry(day)
            if entry is not None and edits.relink_journal(entry.frontmatter, source, target):
                v.rewrite_journal_entry(
                    day, entry.frontmatter, f"journal: {day.isoformat()} relink {source}"
                )
        current = v.read_yaml(rank_flow.RANKINGS_FILE, {})
        if edits.relink_rankings(current, source, target):
            v.write_yaml(
                rank_flow.RANKINGS_FILE, current, f"rankings: merge {source} into {target}"
            )
        return outcome
    if kind == "journal":
        day = date.fromisoformat(str(proposal.get("date")))
        entry = v.read_journal_entry(day)
        if entry is None:
            raise edits.EditError(f"no journal entry for {day}")
        known = {n["id"] for n in v.read_places()}
        outcome = edits.edit_journal(entry.frontmatter, proposal, known)
        v.rewrite_journal_entry(day, entry.frontmatter, f"journal: {day.isoformat()} (edit)")
        return f"{day.isoformat()}: {outcome}"
    if kind == "location":
        location = v.read_location()
        outcome = edits.edit_location(location, proposal)
        v.write_yaml("location.yaml", location, f"location: {outcome}")
        return outcome
    if kind == "goal":
        data = v.read_goals()
        outcome = edits.edit_goal(data, proposal, today)
        v.write_goals(data, f"goals: {outcome}")
        return outcome
    if kind == "focus":
        profile = v.read_profile() or {}
        outcome = edits.edit_focus(profile, proposal)
        v.write_profile(profile, f"profile: {outcome}")
        return outcome
    raise edits.EditError(f"unknown edit kind {kind!r}")


def apply_proposal(itinerary_data: Any, proposal: dict[str, Any], on: Any) -> str:
    """Apply a confirmed chat proposal. The tap *is* the confirmation, so a hard
    date is applied directly rather than asked about a second time."""
    change = {
        k: proposal[k]
        for k in ("id", "place", "place_id", "status", "new_date", "why", "notes")
        if proposal.get(k)
    }
    applied, pending = itinerary.apply_itinerary_changes(itinerary_data, [change], on=on)
    for p in pending:
        itinerary.apply_confirmed_change(itinerary_data, p, on=on)
    return "; ".join([a.summary for a in applied] + [f"{p.place}: {p.new_date}" for p in pending])
