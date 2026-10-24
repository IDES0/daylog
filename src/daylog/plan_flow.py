"""The planner's vault side: its context, saving a plan, applying a choice.

The plan is saved to plans/YYYY-MM-DD.md and added to the assistant's
history, so "make option B a week shorter" in chat has the plan as context.
Choosing an option applies its legs as planned itinerary windows and
records the choice in the plan file (the brief reads the latest one).
"""

from __future__ import annotations

import logging
import threading
from datetime import date, datetime, timedelta

from daylog import (
    brief,
    disciplines,
    goals,
    itinerary,
    llm,
    planner,
    trail,
    wishlist_flow,
)
from daylog.places import PlaceIndex
from daylog.vault import Vault, VaultError

logger = logging.getLogger(__name__)

PLAN_ESTIMATE_USD = 0.60


def build_context(v: Vault, now: datetime) -> str:
    today = now.date()
    index = PlaceIndex(v.read_places())
    location_data = v.read_location()
    node = index.current_place(brief.current_location(location_data))
    where = index.path_name(node["id"]) if node else "unknown"

    goal_lines = []
    for g in v.read_goals():
        if g.get("status", "active") != "active":
            continue
        target = goals.current_target_date(g)
        progress = f", progress {g.get('progress', 0):g} {g['metric']}" if g.get("metric") else ""
        when = f", {'deadline' if g.get('type') == 'hard' else 'target'} {target}" if target else ""
        notes = f" — {g['notes']}" if g.get("notes") else ""
        goal_lines.append(
            f"- {g['id']}: {g.get('title')} ({g.get('type', 'soft')}{progress}{when}){notes}"
        )

    itin = v.read_itinerary()
    hard = [
        f"- {e.get('place')}: {itinerary.current_date(e)}"
        for e in itin
        if e.get("type") == "hard" and e.get("status") not in ("done", "dropped")
    ]
    wish_blocks = []
    dossiers = wishlist_flow.dossiers_for(v, itin)
    for e in wishlist_flow.active_entries(itin):
        label = str(e.get("place", e["id"]))
        head = (
            f"### {label} (itinerary_id {e['id']}, place_id {e.get('place_id', '-')}, "
            f"status {e.get('status')}, for {', '.join(e.get('why') or []) or '-'})"
        )
        wish_blocks.append(head + "\n" + (dossiers.get(label, "(not researched yet)")[:4000]))

    start = today - timedelta(days=30)
    entries = v.read_journal_range(start, today)
    trail_text = trail.format_trail(
        trail.build(
            location_data, {d: e.frontmatter for d, e in entries.items()}, {}, start, today
        ),
        index,
    )
    recent = brief.recent_journal_summaries(v, today + timedelta(days=1), days=7)
    profile = v.read_profile() or {}
    profile_lines = (
        [f"- {k}: {val}" for k, val in profile.items()] if isinstance(profile, dict) else []
    )
    return (
        f"Date reference:\n{brief._date_reference_table(today, days=60)}\n\n"
        f"Currently in: {where}\n\n"
        f"Profile:\n{chr(10).join(profile_lines) or '(none)'}\n\n"
        f"Their own operating principles (weigh plans against these):\n"
        f"{v.read_principles().strip() or '(none written)'}\n\n"
        f"Goals:\n{chr(10).join(goal_lines) or '(none)'}\n\n"
        f"Disciplines:\n{disciplines.summary(v, today)}\n\n"
        f"Hard deadlines:\n{chr(10).join(hard) or '(none recorded)'}\n\n"
        f"Last 30 days:\n{trail_text}\n\n"
        f"Recent journal:\n{recent}\n\n"
        f"Wishlist with research:\n\n{chr(10).join(wish_blocks) or '(empty)'}"
    )


# The newest plan made in this process, by its file: the web app reads its
# options back (the markdown alone can't be turned back into options).
_recent_lock = threading.Lock()
_recent: dict[str, planner.Plan] = {}


def generate(v: Vault, now: datetime) -> tuple[planner.Plan, str, str]:
    """Run the planner and save the plan. Returns (plan, markdown, vault path);
    a plan without options is not saved (its situation says why)."""
    plan = planner.run(build_context(v, now))
    text = planner.render(plan, now.date())
    path = f"plans/{now.date().isoformat()}.md"
    if not plan.options:
        llm.flush(v)
        return plan, text, path
    try:
        v.write_text(path, text, f"plan: {now.date().isoformat()}")
    except VaultError:
        logger.exception("saving plan failed")
    llm.flush(v)
    with _recent_lock:
        _recent.clear()
        _recent[path] = plan
    return plan, text, path


def recent_plan(path: str) -> planner.Plan | None:
    with _recent_lock:
        return _recent.get(path)


def choose(
    v: Vault, plan: planner.Plan, path: str, index: int, today: date
) -> tuple[str, list[str]]:
    """Apply option `index`: its legs become planned itinerary windows, and the
    plan file records the choice. Returns (label, legs applied); raises VaultError."""
    option = plan.options[index]
    data = v.read_itinerary()
    applied = planner.apply_option(data, option, today)
    v.write_itinerary(data, f"itinerary: plan {option.get('label')}")
    existing = v.read_text(path) or ""
    v.write_text(
        path,
        existing + f"\n**Chosen: {chr(65 + index)}. {option.get('label')}**\n",
        f"plan: chose {option.get('label')}",
    )
    with _recent_lock:
        _recent.pop(path, None)
    return str(option.get("label")), applied


def latest_plan(v: Vault) -> str | None:
    docs = v.list_docs("plans")
    return v.read_text(docs[-1]) if docs else None
