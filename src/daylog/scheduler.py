"""The scheduled jobs and their clock: morning brief, end-of-day reconcile, weekly review.

Each job leaves its result in the app (the brief at GET /api/brief, the
review under reviews/, a plan's 'Which option?' card in the card store) and
sends at most one notification per event to the user's own devices
(push.py) — never to anyone else. Times are local (TZ): BRIEF_HOUR (7),
DAY_CUTOFF_HOUR (4, reconcile at :05) and REVIEW_HOUR (20, Sundays; the
weekly plan follows unless WEEKLY_PLAN=0).
"""

from __future__ import annotations

import logging
import os
from collections.abc import Callable
from datetime import datetime
from typing import Any

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger

from daylog import daily, llm, plan_flow, push, runtime, services
from daylog.vault import Vault
from daylog.webapp.api import Api

logger = logging.getLogger(__name__)

# A job that couldn't run on time (the app was restarting) still runs if
# it's this late; later than that it waits for its next slot.
MISFIRE_GRACE_S = 3 * 3600


def brief_hour() -> int:
    return int(os.environ.get("BRIEF_HOUR", "7"))


def _plural(n: int, word: str) -> str:
    return f"{n} {word}{'s' if n != 1 else ''}"


def _setup(v: Vault | None, now: datetime | None) -> tuple[Vault, datetime]:
    return v or runtime.vault(), now or datetime.now(runtime.tz())


# -- the jobs --------------------------------------------------------------------------


def morning_brief(api: Api, v: Vault | None = None, now: datetime | None = None) -> None:
    """Build and save the brief, notify it, then say how many decisions wait."""
    v, now = _setup(v, now)
    today = now.date()
    try:
        text = services.build_brief(v, today)
    except Exception:
        logger.exception("failed to generate brief")
        push.send(v, "Brief failed", "Building today's brief failed. Check the logs.", "/#/home")
        return
    finally:
        llm.flush(v)
    api.chat.remember("The app sent the morning brief", text)
    title = f"☀️ Brief · {today.strftime('%a %b')} {today.day}"
    push.send(v, title, push.lock_screen(text), "/#/home")
    # Decisions the morning research traced: cards until they're answered.
    waiting = api.waiting_decisions(v)
    if waiting:
        push.send(
            v,
            f"{_plural(waiting, 'decision')} waiting",
            "What it would change, on one card. Nothing moves until you confirm.",
            "/#/home",
        )


def end_of_day(api: Api, v: Vault | None = None, now: datetime | None = None) -> None:
    """At the day cutoff: reconcile the day that ended (and recent misses).
    Quiet unless it corrected a goal total."""
    v, now = _setup(v, now)
    outcomes = daily.reconcile_recent(v, daily.logical_day(now))
    notable = [o for o in outcomes if "corrected" in o]
    if notable:
        body = push.lock_screen("\n".join(notable))
        push.send(v, "Goal totals corrected", body, "/#/you")


def weekly(api: Api, v: Vault | None = None, now: datetime | None = None) -> None:
    """Sunday evening: the week's review, then (budget allowing) a fresh plan."""
    v, now = _setup(v, now)
    try:
        written = daily.write_review(v, daily.logical_day(now))
    except Exception:
        logger.exception("weekly review failed")
        push.send(v, "Weekly review failed", "Writing the review failed. Check the logs.", "/#/you")
        return
    if written is None:
        logger.info("no journal entries this week; no review")
        return
    label, _start, _end, text = written
    api.chat.remember("The app wrote the weekly review", text)
    plan = daily.weekly_plan_enabled()
    affordable = plan and llm.can_spend(v, plan_flow.PLAN_ESTIMATE_USD)
    body = push.lock_screen(text)
    if plan and not affordable:
        body = push.lock_screen("No plan this week: the monthly budget is spent.\n" + text)
    push.send(v, f"Weekly review is ready · {label}", body, "/#/you")
    if affordable:
        weekly_plan(api, v, now)


def weekly_plan(api: Api, v: Vault, now: datetime) -> None:
    """Make a plan and leave its option card in the app; one notification."""
    try:
        plan, text, path = plan_flow.generate(v, now)
    except Exception:
        logger.exception("planner failed")
        push.send(v, "Planning failed", "The weekly plan failed. Check the logs.", "/#/trips")
        return
    if not plan.options:
        push.send(v, "No plan this week", push.lock_screen(plan.situation), "/#/trips")
        return
    api.chat.remember("The app made a travel plan", text)
    api.plan_card(plan, path)
    push.send(
        v,
        "A plan for the next weeks",
        f"{_plural(len(plan.options), 'option')} to pick from.",
        "/#/trips",
    )


# -- the clock -------------------------------------------------------------------------


def jobs(api: Api) -> list[tuple[str, Callable[[], None], dict[str, Any]]]:
    """(id, callable, cron fields) for every scheduled job."""
    return [
        ("brief", lambda: morning_brief(api), {"hour": brief_hour(), "minute": 0}),
        (
            "reconcile",
            lambda: end_of_day(api),
            {"hour": daily.day_cutoff_hour(), "minute": 5},
        ),
        (
            "weekly",
            lambda: weekly(api),
            {"day_of_week": "sun", "hour": daily.review_hour(), "minute": 0},
        ),
    ]


def build(api: Api) -> Any:
    """A BackgroundScheduler (not started) with every job, in local time."""
    tz = runtime.tz()
    scheduler = BackgroundScheduler(timezone=tz)
    for job_id, func, fields in jobs(api):
        scheduler.add_job(
            func,
            CronTrigger(timezone=tz, **fields),
            id=job_id,
            name=job_id,
            coalesce=True,
            max_instances=1,
            misfire_grace_time=MISFIRE_GRACE_S,
        )
    return scheduler
