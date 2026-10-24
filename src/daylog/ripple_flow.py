"""Proposals: a decision's changes on one card.

The morning research routine reads new `decisions` in the journal, traces
each through the itinerary, goals and plan, and writes a proposal file
(see proposals.py). The web app shows every pending proposal as one card
(and the morning brief's notification says how many wait): apply every
change (and the revised plan, if there is one), go through them one by one
as ordinary confirm cards, or skip. The file records what was chosen, so a
proposal is shown until it's answered.

POST /api/ripple does the same analysis on the spot with the app's own
model, for when the morning is too long to wait.
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Any

from daylog import chat_flow, edits, itinerary, llm, plan_flow, proposals, ripple
from daylog.runtime import tz
from daylog.vault import Vault, VaultError

logger = logging.getLogger(__name__)

RIPPLE_ESTIMATE_USD = 0.50
PLAN_CHARS = 6000


def _itinerary_lines(itin: Any) -> list[str]:
    lines = []
    for e in itin or []:
        if e.get("status") in ("done", "dropped"):
            continue
        when = itinerary.current_date(e)
        window = e.get("target_window")
        if isinstance(window, list) and len(window) == 2:
            when = f"{window[0]} – {window[1]}"
        notes = f" — {e['notes']}" if e.get("notes") else ""
        lines.append(
            f"- {e['id']}: {e.get('place')} ({e.get('type', 'soft')}, {e.get('status')}"
            f"{', ' + str(when) if when else ''}){notes}"
        )
    return lines


def build_context(v: Vault, now: datetime, decision: str) -> str:
    plan = plan_flow.latest_plan(v) or "(no plan saved)"
    return (
        f"The decision, in their words:\n{decision}\n\n"
        f"Full itinerary (active entries):\n"
        f"{chr(10).join(_itinerary_lines(v.read_itinerary())) or '(empty)'}\n\n"
        f"{plan_flow.build_context(v, now)}\n\n"
        f"Their current plan:\n{plan[:PLAN_CHARS]}"
    )


def ids(v: Vault) -> tuple[set[str], set[str]]:
    return (
        {str(e["id"]) for e in v.read_itinerary() or []},
        {str(g["id"]) for g in v.read_goals() or []},
    )


def analyze(v: Vault, now: datetime, decision: str) -> str:
    """/ripple: run the analysis, save it and a proposal file. Returns the proposal path."""
    result = ripple.run(build_context(v, now, decision))
    result.changes = ripple.valid_changes(result.changes, *ids(v))
    stem = f"{now.date().isoformat()}-{ripple.slug(result.decision)}"
    analysis = f"research/decisions/{stem}.md"
    v.write_text(analysis, ripple.render(result, now.date()), f"decisions: {result.decision}")
    path = f"{proposals.FOLDER}/{stem}.yaml"
    data = proposals.build(result, now.date().isoformat(), analysis)
    v.write_yaml(path, data, f"proposals: {result.decision}")
    return path


def pending(v: Vault) -> list[str]:
    return [
        p
        for p in v.list_docs(proposals.FOLDER, "*.yaml")
        if proposals.is_pending(v.read_yaml(p, None))
    ]


def open_card(v: Vault, path: str) -> tuple[str, list[Any], bool] | None:
    """(card text, changes, has a draft plan) for a pending proposal, or None when
    it's answered or has nothing left to change (then it's marked reviewed)."""
    data = v.read_yaml(path, None)
    if not proposals.is_pending(data):
        return None
    changes = proposals.changes_for(data, *ids(v))
    if not changes and not data.get("plan_draft"):
        mark(v, path, "reviewed", ["nothing left to change"])
        return None
    return proposals.card_text(data, changes), changes, bool(data.get("plan_draft"))


def apply_label(n: int, has_plan: bool) -> str:
    return f"Apply all {n}" + (" + new plan" if has_plan else "") if n else "Use the new plan"


def review_each(v: Vault, path: str) -> tuple[list[Any], bool]:
    """'One by one': the changes to confirm separately and whether a draft plan
    waits; the proposal is marked reviewed."""
    data = v.read_yaml(path, {}) or {}
    changes = proposals.changes_for(data, *ids(v))
    mark(v, path, "reviewed", [])
    return changes, bool(data.get("plan_draft"))


def adopt(v: Vault, path: str, today: str) -> str | None:
    """'Use it': make the proposal's draft plan the current one."""
    return adopt_plan(v, v.read_yaml(path, {}) or {}, today)


def trace(v: Vault, now: datetime, decision: str) -> tuple[str, str]:
    """/ripple's work: analyze, save, and return (proposal path, analysis text)."""
    try:
        path = analyze(v, now, decision)
    finally:
        llm.flush(v)
    data = v.read_yaml(path, {}) or {}
    return path, v.read_text(str(data.get("analysis", ""))) or ""


def mark(v: Vault, path: str, status: str, results: list[str]) -> None:
    data = v.read_yaml(path, {}) or {}
    data["status"] = status
    data["answered_on"] = datetime.now(tz()).date().isoformat()
    if results:
        data["results"] = results
    v.write_yaml(path, data, f"proposals: {status} {path.rsplit('/', 1)[-1]}")


def adopt_plan(v: Vault, data: dict[str, Any], today: str) -> str | None:
    """Make the proposal's draft plan the current one (the newest file in plans/)."""
    draft = data.get("plan_draft")
    text = v.read_text(str(draft)) if draft else None
    if not text:
        return None
    path = f"plans/{today}-plan.md"
    v.write_text(path, text, f"plans: adopted {draft}")
    return path


def apply_all(v: Vault, changes: list[dict[str, Any]], today: Any) -> tuple[list[str], list[str]]:
    done, failed = [], []
    for change in changes:
        try:
            done.append(chat_flow.apply_edit(v, change, today) or change["summary"])
        except (edits.EditError, ValueError) as exc:
            failed.append(f"{change['summary']}: {exc}")
        except VaultError:
            logger.exception("applying a proposed change failed")
            failed.append(f"{change['summary']}: saving failed")
    return done, failed


def apply_proposal(v: Vault, path: str) -> str:
    data = v.read_yaml(path, {}) or {}
    today = datetime.now(tz()).date()
    done, failed = apply_all(v, proposals.changes_for(data, *ids(v)), today)
    plan = adopt_plan(v, data, today.isoformat())
    if plan:
        done.append(f"current plan is now {plan}")
    mark(v, path, "applied", done + [f"failed: {f}" for f in failed])
    lines = [f"Applied {len(done)}:", *[f"• {d}" for d in done]]
    if failed:
        lines += [f"Didn't apply {len(failed)}:", *[f"• {f}" for f in failed]]
    return "\n".join(lines)
