"""The web app's JSON API: the route table and its handlers (docs/web-api.md).

Every handler returns (status, dict); calendar_server.py does the HTTP.
Before any handler runs: the session (401 signed_out), then for anything
but GET the CSRF rule (header `X-Daylog: 1`, and an Origin, when sent,
equal to the host; 403 otherwise). No CORS headers are ever sent.

Writes go through the same service functions as Telegram, and anything
inferred comes back as a confirm card (cards.py), never applied directly.
The bot still never contacts anyone: nothing here sends a message.

Never log request bodies, passwords or transcripts.
"""

from __future__ import annotations

import json
import logging
import os
import re
import tempfile
import threading
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from importlib import resources
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from daylog import (
    analytics,
    brief,
    chat,
    chat_flow,
    daily,
    dashboard,
    disciplines,
    edits,
    itinerary,
    journal_service,
    llm,
    plan_flow,
    rank_flow,
    rankings,
    research,
    research_flow,
    review,
    ripple_flow,
    services,
    timeline,
    transcribe,
    wishlist_flow,
)
from daylog.disciplines import Graph
from daylog.places import PlaceIndex
from daylog.tg import tz
from daylog.vault import CorrectionConflictError, Vault, VaultError
from daylog.webapp.cards import Card, CardStore, Outcome, UnknownAction, action
from daylog.webapp.chat_store import ChatStore

logger = logging.getLogger(__name__)

MAX_JSON_BYTES = 64 * 1024
MAX_VOICE_BYTES = 20 * 1024 * 1024
JOURNAL_DEFAULT_DAYS = 14
JOURNAL_MAX_DAYS = 366
STATIC_FILES = {
    "index.html": "text/html; charset=utf-8",
    "app.js": "text/javascript; charset=utf-8",
    "app.css": "text/css; charset=utf-8",
}
_VOICE_SUFFIX = {"audio/webm": ".webm", "audio/mp4": ".m4a", "audio/ogg": ".ogg"}

Reply = tuple[int, dict[str, Any]]


def static_file(name: str) -> bytes | None:
    """One of the app shell's files from the package (static/), or None."""
    if name not in STATIC_FILES:
        return None
    try:
        return (resources.files("daylog.webapp") / "static" / name).read_bytes()
    except (FileNotFoundError, NotADirectoryError):
        return None


def default_vault() -> Vault:
    """The vault, caught up with the remote (throttled — see Vault.sync)."""
    v = Vault(Path(os.environ.get("VAULT_PATH", "../daylog-vault")))
    v.sync()
    return v


@dataclass
class Request:
    method: str
    path: str
    query: Mapping[str, list[str]] = field(default_factory=dict)
    # Lower-cased names.
    headers: Mapping[str, str] = field(default_factory=dict)
    body: bytes = b""
    signed_in: bool = False

    def arg(self, name: str, default: str = "") -> str:
        return (self.query.get(name) or [default])[0]


class ApiError(Exception):
    def __init__(self, status: int, code: str, message: str = "") -> None:
        super().__init__(code)
        self.status, self.code, self.message = status, code, message

    def reply(self) -> Reply:
        body = {"error": self.code}
        if self.message:
            body["message"] = self.message
        return self.status, body


def csrf_ok(method: str, headers: Mapping[str, str]) -> bool:
    """A non-GET request must carry X-Daylog: 1, and an Origin (when sent) must be
    this host (or the proxy's forwarded host)."""
    if method == "GET":
        return True
    if headers.get("x-daylog") != "1":
        return False
    origin = headers.get("origin")
    if origin is None:
        return True
    netloc = urlsplit(origin).netloc
    hosts = {h for h in (headers.get("host"), headers.get("x-forwarded-host")) if h}
    return bool(netloc) and netloc in hosts


def body_limit(path: str) -> int:
    return MAX_VOICE_BYTES if path == "/api/voice" else MAX_JSON_BYTES


def plain(value: Any) -> Any:
    """Vault data (ruamel maps, dates) as JSON-ready values."""
    if isinstance(value, datetime | date):
        return value.isoformat()
    if isinstance(value, Mapping):
        return {str(k): plain(v) for k, v in value.items()}
    if isinstance(value, list | tuple):
        return [plain(v) for v in value]
    if value is None or isinstance(value, bool | int | float | str):
        return value
    return str(value)


def _date(value: Any, name: str = "date") -> date:
    try:
        return date.fromisoformat(str(value))
    except ValueError as exc:
        raise ApiError(400, "bad_date", f"{name} must be YYYY-MM-DD") from exc


def _int(value: str, default: int, low: int = 1, high: int = 3650) -> int:
    if not value:
        return default
    if not value.isdigit():
        raise ApiError(400, "bad_number", f"expected a number, got {value[:20]!r}")
    return max(low, min(high, int(value)))


def _text_field(data: Mapping[str, Any], name: str, limit: int = 20000) -> str:
    value = data.get(name)
    if not isinstance(value, str) or not value.strip():
        raise ApiError(400, "missing_field", f"'{name}' is required")
    return value.strip()[:limit]


Handler = Callable[[Request, re.Match[str]], Reply]


class Api:
    """One per server: the routes, the open cards and the web chat."""

    def __init__(
        self,
        vault: Callable[[], Vault] = default_vault,
        now: Callable[[], datetime] | None = None,
        spawn: Callable[[Callable[[], None]], None] | None = None,
    ) -> None:
        self._vault = vault
        self._now = now or (lambda: datetime.now(tz()))
        self._spawn = spawn or _spawn_thread
        self.cards = CardStore()
        self.chat = ChatStore()
        gets: list[tuple[str, Handler]] = [
            (r"/api/me", self.me),
            (r"/api/home", self.home),
            (r"/api/map", self.map),
            (r"/api/calendar", self.calendar),
            (r"/api/journal", self.journal),
            (r"/api/journal/(\d{4}-\d{2}-\d{2})", self.journal_day),
            (r"/api/chat", self.chat_history),
            (r"/api/brief", self.brief),
            (r"/api/surf", self.surf),
            (r"/api/fly", self.fly),
            (r"/api/upcoming", self.upcoming),
            (r"/api/status", self.status),
            (r"/api/usage", self.usage),
            (r"/api/site", self.site),
            (r"/api/plan", self.plan),
            (r"/api/trail", self.trail),
            (r"/api/places", self.places),
            (r"/api/places/([A-Za-z0-9_.-]+)", self.place),
            (r"/api/wishlist", self.wishlist),
            (r"/api/rankings", self.rankings),
            (r"/api/review", self.review),
            (r"/api/disciplines", self.disciplines),
            (r"/api/changes", self.changes),
            (r"/api/cards", self.open_cards),
        ]
        posts: list[tuple[str, Handler]] = [
            (r"/api/log", self.log),
            (r"/api/voice", self.voice),
            (r"/api/reconcile", self.reconcile),
            (r"/api/chat", self.chat_send),
            (r"/api/chat/reset", self.chat_reset),
            (r"/api/brief", self.make_brief),
            (r"/api/plan", self.make_plan),
            (r"/api/ripple", self.ripple),
            (r"/api/wishlist", self.want),
            (r"/api/research", self.research),
            (r"/api/review", self.make_review),
            (r"/api/changes/([0-9a-f]{4,40})/revert", self.revert),
            (r"/api/cards/(c_[0-9a-f]+)", self.answer_card),
        ]
        self._routes: list[tuple[str, re.Pattern[str], Handler]] = [
            ("GET", re.compile(p), h) for p, h in gets
        ] + [("POST", re.compile(p), h) for p, h in posts]

    # -- dispatch ---------------------------------------------------------------

    def precheck(self, req: Request) -> Reply | None:
        """Session first, then CSRF; run before the body is even read."""
        if not req.signed_in:
            return 401, {"error": "signed_out"}
        if not csrf_ok(req.method, req.headers):
            return 403, {"error": "forbidden", "message": "Missing X-Daylog header or bad Origin."}
        return None

    def handle(self, req: Request) -> Reply:
        refused = self.precheck(req)
        if refused is not None:
            return refused
        for method, pattern, handler in self._routes:
            match = pattern.fullmatch(req.path)
            if match and method == req.method:
                try:
                    return handler(req, match)
                except ApiError as exc:
                    return exc.reply()
                except Exception:
                    logger.exception("api %s %s failed", req.method, req.path)
                    return 500, {
                        "error": "server_error",
                        "message": "Something went wrong — check the server logs.",
                    }
        if any(p.fullmatch(req.path) for _, p, _ in self._routes):
            return 405, {"error": "method_not_allowed"}
        return 404, {"error": "not_found"}

    # -- helpers ------------------------------------------------------------------

    def _today(self) -> date:
        return daily.logical_day(self._now())

    @staticmethod
    def _json(req: Request) -> dict[str, Any]:
        if not req.body.strip():
            return {}
        try:
            data = json.loads(req.body)
        except ValueError as exc:
            raise ApiError(400, "bad_json", "The body isn't valid JSON.") from exc
        if not isinstance(data, dict):
            raise ApiError(400, "bad_json", "The body must be a JSON object.")
        return data

    def _cards(self, cards: list[Card]) -> list[dict[str, Any]]:
        return [c.to_json() for c in cards]

    def _budget(self, v: Vault, estimate: float) -> None:
        if not llm.can_spend(v, estimate):
            raise ApiError(402, "budget", "The monthly API budget is spent.")

    # -- session / home -----------------------------------------------------------

    def me(self, req: Request, m: re.Match[str]) -> Reply:
        return 200, {"signed_in": True, "today": self._today().isoformat(), "tz": str(tz())}

    def home(self, req: Request, m: re.Match[str]) -> Reply:
        v = self._vault()
        today = self._today()
        location = v.read_location() or []
        index = PlaceIndex(v.read_places())
        current = brief.current_location(location)
        where = None
        if current:
            since = dashboard._as_date(current.get("from"))
            node = index.current_place(current)
            where = {
                "place": str(current.get("place", "")),
                "since": since.isoformat() if since else None,
                "day": (today - since).days + 1 if since else None,
                "path": index.path_name(node["id"]) if node else None,
            }

        legs = []
        for e in v.read_itinerary() or []:
            ahead = dashboard._ahead(e, today)
            if ahead is not None:
                legs.append(
                    {
                        "place": str(e.get("place", e.get("id", "?"))),
                        "when": ahead[1],
                        "date": ahead[0].isoformat(),
                        "status": e.get("status"),
                        "type": e.get("type", "soft"),
                    }
                )
        legs.sort(key=lambda leg: leg["date"])

        goal_list = []
        for g in v.read_goals() or []:
            if not isinstance(g, dict) or g.get("status", "active") != "active":
                continue
            progress, target = (
                dashboard._number(g.get("progress")),
                dashboard._number(g.get("target")),
            )
            goal_list.append(
                {
                    "id": g.get("id"),
                    "title": str(g.get("title", g.get("id", "?"))),
                    "type": g.get("type", "soft"),
                    "metric": g.get("metric"),
                    "progress": progress,
                    "target": target,
                    "percent": min(100, round(100 * progress / target))
                    if progress is not None and target
                    else None,
                    "when": dashboard._when(g)[1],
                    "slips": len(g.get("slip_history") or []),
                    "discipline": g.get("discipline"),
                    "notes": g.get("notes"),
                }
            )

        dates = v.list_journal_dates()
        everything = v.read_journal_range(min(dates), today) if dates else {}
        start = today - timedelta(days=dashboard.RECENT_DAYS - 1)
        stats = review.week_stats({d: e for d, e in everything.items() if d >= start})
        week = {
            "days_logged": stats["days_logged"],
            "of": dashboard.RECENT_DAYS,
            "activities": [
                {"label": kind, "days": n, "hours": stats["activity_hours"].get(kind)}
                for kind, n in stats["activity_days"].items()
            ],
            "energy": stats["avg_energy"],
            "mood": stats["avg_mood"],
            "focus": stats["avg_focus"],
            "kcal": stats["avg_kcal_per_day"],
            "protein_g": stats["avg_protein_g_per_day"],
        }
        milestones = list(
            reversed(review.week_stats(everything)["milestones"][-dashboard.MAX_MILESTONES :])
        )
        entry = everything.get(today)
        today_entry = (
            {
                "summary": entry.summary,
                "activities": plain(entry.frontmatter.get("activities") or []),
            }
            if entry
            else None
        )
        self._sync_proposals(v)
        return 200, {
            "today": today.isoformat(),
            "weekday": today.strftime("%A"),
            "where": where,
            "next": legs[:8],
            "goals": goal_list,
            "week": week,
            "milestones": milestones,
            "today_entry": today_entry,
            "open_cards": len(self.cards.open()),
        }

    def map(self, req: Request, m: re.Match[str]) -> Reply:
        v = self._vault()
        data = dashboard.future_map(
            v.read_itinerary(), PlaceIndex(v.read_places()), v.read_location(), self._today()
        )
        return 200, plain(data)

    def calendar(self, req: Request, m: re.Match[str]) -> Reply:
        v = self._vault()
        today = self._today()
        week_of = _date(req.arg("week"), "week") if req.arg("week") else today
        monday = dashboard.week_start(min(week_of, today))
        days = [monday + timedelta(days=i) for i in range(7)]
        entries = v.read_journal_range(monday, days[-1])
        out = []
        for d in days:
            entry = entries.get(d)
            blocks = timeline.day_blocks(entry.frontmatter) if entry else []
            out.append(
                {
                    "date": d.isoformat(),
                    "summary": entry.summary if entry else None,
                    "blocks": [
                        {
                            "label": b.kind,
                            "detail": b.detail,
                            "start": b.start,
                            "hours": round(b.end - b.start, 2),
                            "guess": b.guessed,
                            "hue": dashboard._hue(b.kind),
                        }
                        for b in blocks
                    ],
                }
            )
        nxt = monday + timedelta(days=7)
        return 200, {
            "week_of": monday.isoformat(),
            "days": out,
            "prev": (monday - timedelta(days=7)).isoformat(),
            "next": nxt.isoformat() if nxt <= today else None,
        }

    # -- journal ----------------------------------------------------------------------

    @staticmethod
    def _day(d: date, entry: Any) -> dict[str, Any]:
        fm = entry.frontmatter
        return {
            "date": d.isoformat(),
            "summary": entry.summary,
            "activities": plain(fm.get("activities") or []),
            "meals": plain(fm.get("meals") or []),
            "felt": plain(fm.get("felt") or []),
            "milestones": plain(fm.get("milestones") or []),
            "location": plain(fm.get("location")),
            "notes": journal_service.note_count(entry.summary),
        }

    def journal(self, req: Request, m: re.Match[str]) -> Reply:
        v = self._vault()
        end = _date(req.arg("to"), "to") if req.arg("to") else self._today()
        start = (
            _date(req.arg("from"), "from")
            if req.arg("from")
            else end - timedelta(days=JOURNAL_DEFAULT_DAYS - 1)
        )
        if start > end:
            raise ApiError(400, "bad_range", "'from' is after 'to'.")
        start = max(start, end - timedelta(days=JOURNAL_MAX_DAYS - 1))
        entries = v.read_journal_range(start, end)
        return 200, {"days": [self._day(d, e) for d, e in sorted(entries.items(), reverse=True)]}

    def journal_day(self, req: Request, m: re.Match[str]) -> Reply:
        day = _date(m.group(1))
        entry = self._vault().read_journal_entry(day)
        if entry is None:
            raise ApiError(404, "not_found", f"No entry for {day.isoformat()}.")
        return 200, {**self._day(day, entry), "transcript": entry.transcript}

    def log(self, req: Request, m: re.Match[str]) -> Reply:
        data = self._json(req)
        text = _text_field(data, "text")
        day = _date(data["date"]) if data.get("date") else None
        return self._log(text, day)

    def voice(self, req: Request, m: re.Match[str]) -> Reply:
        if not req.body:
            raise ApiError(400, "empty", "No audio in the request.")
        day = _date(req.arg("date")) if req.arg("date") else None
        kind = req.headers.get("content-type", "").split(";")[0].strip().lower()
        try:
            with tempfile.TemporaryDirectory() as tmp_dir:
                audio_path = Path(tmp_dir) / f"voice{_VOICE_SUFFIX.get(kind, '.webm')}"
                audio_path.write_bytes(req.body)
                transcript = transcribe.transcribe(audio_path)
        except Exception:
            logger.exception("failed to transcribe a web voice note")
            raise ApiError(
                500, "transcribe_failed", "Couldn't transcribe that. Nothing was saved."
            ) from None
        if not transcript:
            raise ApiError(422, "no_speech", "Couldn't make out any speech in that.")
        status, body = self._log(transcript, day)
        body["transcript"] = transcript
        return status, body

    def _log(self, text: str, day: date | None) -> Reply:
        now = self._now()
        today = daily.logical_day(now)
        if day is not None and day > today:
            raise ApiError(400, "bad_date", "Can't log a day that hasn't happened yet.")
        v = self._vault()
        try:
            result = journal_service.log(v, text, day or today, now, backdated=day is not None)
        except Exception:
            logger.exception("failed to process a web entry")
            return 500, {
                "error": "log_failed",
                "message": "Something went wrong logging that, so nothing was saved.",
                "text": text,
            }
        cards = self._log_cards(result)
        if result.location_changed:
            self._spawn(self._arrive_in_background)
        if result.place_mentions:
            self._spawn(lambda: self._mentions_in_background([result.date]))
        if not result.saved:
            return 500, {
                "error": "commit_failed",
                "message": f"Wrote {result.date.isoformat()} to the vault, but the git commit "
                "failed — it's on disk, just not committed yet.",
                "cards": self._cards(cards),
            }
        return 200, {
            "date": result.date.isoformat(),
            "summary": result.summary,
            "notes": result.notes,
            "cards": self._cards(cards),
        }

    def reconcile(self, req: Request, m: re.Match[str]) -> Reply:
        data = self._json(req)
        day = _date(data["date"]) if data.get("date") else self._today()
        v = self._vault()
        try:
            message = daily.run_reconcile(v, day, force=True)
        finally:
            llm.flush(v)
        return 200, {"message": message}

    # -- cards made by a note -----------------------------------------------------------

    def _log_cards(self, result: journal_service.LogResult) -> list[Card]:
        cards = [self._slip_card(s) for s in result.slips]
        cards += [self._change_card(c) for c in result.changes]
        cards += [self._correction_card(c) for c in result.corrections]
        cards += [
            card
            for g in result.guesses
            if (card := self._fix_card(result.graph, result.date, g)) is not None
        ]
        cards += [self._add_card(result.graph, p) for p in result.new_disciplines]
        return cards

    def _slip_card(self, slip: Any) -> Card:
        def apply(answer: str) -> Outcome:
            if answer == "cancel":
                return Outcome(f"Cancelled — '{slip.title}' deadline unchanged.")
            try:
                journal_service.confirm_slip(self._vault(), slip, self._today())
            except VaultError:
                logger.exception("goals commit failed confirming slip for %s", slip.goal_id)
                return Outcome(f"Saving failed for '{slip.title}' — check the logs.", ok=False)
            return Outcome(f"Confirmed — '{slip.title}' deadline moved to {slip.new_date}.")

        return self.cards.add("goal_slip", journal_service.slip_question(slip), apply)

    def _change_card(self, change: Any) -> Card:
        def apply(answer: str) -> Outcome:
            if answer == "cancel":
                return Outcome(f"Cancelled — '{change.place}' left unchanged.")
            try:
                journal_service.confirm_change(self._vault(), change, self._today())
            except VaultError:
                logger.exception("itinerary commit failed confirming change for %s", change.id)
                return Outcome(f"Saving failed for '{change.place}' — check the logs.", ok=False)
            return Outcome(f"Confirmed — '{change.place}' set to {change.new_date}.")

        return self.cards.add("itinerary", journal_service.change_question(change), apply)

    def _correction_card(self, correction: journal_service.PendingCorrection) -> Card:
        def apply(answer: str) -> Outcome:
            if answer == "cancel":
                return Outcome(f"Cancelled — '{correction.description}' left unchanged.")
            try:
                journal_service.confirm_correction(self._vault(), correction)
            except CorrectionConflictError:
                return Outcome(
                    f"'{correction.description}' no longer matches that entry — it may have "
                    "already changed. Nothing was removed.",
                    ok=False,
                )
            except VaultError:
                logger.exception("correction commit failed for %s", correction.entry_date)
                return Outcome("Saving failed — check the logs.", ok=False)
            return Outcome(
                f"Removed '{correction.description}' from {correction.entry_date.isoformat()}."
            )

        return self.cards.add("correction", journal_service.correction_question(correction), apply)

    def _fix_card(self, graph: Graph, entry_date: date, guess: disciplines.Guess) -> Card | None:
        others = [o for o in guess.options if o != guess.chosen]
        if not others:
            return None
        fix = disciplines.Fix(entry_date, guess.index, guess.said)

        def apply(answer: str) -> Outcome:
            if answer == "cancel":
                return Outcome(f"Kept {graph.name(guess.chosen)}.")
            try:
                fixed = disciplines.apply_fix(self._vault(), fix, answer)
            except VaultError:
                logger.exception("discipline fix commit failed")
                return Outcome("The fix didn't save — check the logs.", ok=False)
            if not fixed:
                return Outcome("That entry changed since — nothing was edited.", ok=False)
            return Outcome(f"Fixed — logged as {graph.name(answer)}.")

        return self.cards.add(
            "discipline_fix",
            f"Not {graph.name(guess.chosen)}? Tap what it was.",
            apply,
            actions=[action(o, graph.name(o)) for o in others]
            + [action("cancel", f"Keep {graph.name(guess.chosen)}")],
        )

    def _add_card(self, graph: Graph, proposal: disciplines.Proposal) -> Card:
        under = ", ".join(graph.name(p) for p in proposal.parents if p in graph) or "the top level"

        def apply(answer: str) -> Outcome:
            if answer == "cancel":
                return Outcome(
                    f"Left {proposal.name} out of the disciplines; the day still logs it."
                )
            try:
                added = disciplines.add_discipline(self._vault(), proposal, answer)
            except VaultError:
                logger.exception("disciplines commit failed")
                return Outcome("Adding it didn't save — check the logs.", ok=False)
            if not added:
                return Outcome(f"{proposal.name} is already a discipline.")
            return Outcome(f"Added {proposal.name} to the disciplines.")

        return self.cards.add(
            "discipline_add",
            f"New discipline: {proposal.name}, under {under}. Add it?",
            apply,
            actions=[
                action("active", "Add (pursuing)", "primary"),
                action("want", "Add (want)"),
                action("cancel", "No"),
            ],
        )

    # -- research cards --------------------------------------------------------------

    def _place_cards(
        self, findings: research.Findings, days: list[date], bulk: bool = False
    ) -> list[Card]:
        if not findings.new_places:
            return []
        v = self._vault()
        index = PlaceIndex(v.read_places())
        batch = research_flow.new_batch(findings, days)
        if bulk and len(findings.new_places) > 3:

            def apply_bulk(answer: str) -> Outcome:
                if answer == "skipall":
                    return Outcome("Skipped all.")
                if answer == "review":
                    return Outcome("One by one:", cards=self._each_place(batch))
                return self._add_places(batch, list(range(len(batch["proposals"]))))

            return [
                self.cards.add(
                    "place",
                    f"{len(findings.new_places)} new places found",
                    apply_bulk,
                    detail="\n".join(research_flow.bulk_lines(index, findings)),
                    actions=[
                        action("addall", "Add all", "primary"),
                        action("review", "Review each"),
                        action("skipall", "Skip all"),
                    ],
                )
            ]
        return self._each_place(batch)

    def _each_place(self, batch: dict[str, Any]) -> list[Card]:
        index = PlaceIndex(self._vault().read_places())
        cards = []
        for i, proposal in enumerate(batch["proposals"]):
            title, _, detail = research_flow.card_text(index, batch, i).partition("\n")

            def apply(answer: str, i: int = i, name: str = str(proposal.get("name"))) -> Outcome:
                if answer == "skip":
                    return Outcome(f"Skipped {name}.")
                return self._add_places(batch, [i])

            cards.append(
                self.cards.add(
                    "place",
                    title.removeprefix("📍 "),
                    apply,
                    detail=detail.strip(),
                    actions=[action("add", "Add", "primary"), action("skip", "Skip")],
                )
            )
        return cards

    def _add_places(self, batch: dict[str, Any], indexes: list[int]) -> Outcome:
        v = self._vault()
        try:
            new_ids = research_flow.add_proposals(v, batch, indexes)
        except VaultError:
            logger.exception("adding places failed")
            return Outcome("Saving failed — nothing was added.", ok=False)
        index = PlaceIndex(v.read_places())
        return Outcome(
            f"Added: {', '.join(index.path_name(p) for p in new_ids) or 'already added'}"
        )

    def _arrive_in_background(self) -> None:
        v = self._vault()
        job = research_flow.plan_arrive(v)
        if job is not None:
            self._place_cards(research_flow.run_job(v, job), [], bulk=True)

    def _mentions_in_background(self, days: list[date]) -> None:
        _, findings = research_flow.lookup_mentions(self._vault(), days)
        if findings is not None:
            self._place_cards(findings, days)

    def _research_job_card(self, job: dict[str, Any]) -> Card | None:
        kind = job.get("job")
        name = str(job.get("place") or "")
        if kind == "explore":
            title = "Research what's around here? (API budget, a minute or two)"
        elif kind == "dossier" and name:
            title = f"Research {name} now? (API budget, a minute or two)"
        else:
            # Ranking is a Telegram flow for now; nothing to confirm here.
            return None

        def apply(answer: str) -> Outcome:
            if answer == "cancel":
                return Outcome("Left it.")
            if kind == "explore":
                return self._explore()
            return self._dossier(name)

        return self.cards.add("research", title, apply)

    def _explore(self) -> Outcome:
        v = self._vault()
        job = research_flow.plan_arrive(v, force=True)
        if job is None:
            return Outcome("Monthly research budget reached.", ok=False)
        findings = research_flow.run_job(v, job)
        text = f"Around {job.where}: {len(findings.new_places)} places found."
        if findings.summary:
            text += f"\n\n{findings.summary}"
        return Outcome(text, cards=self._place_cards(findings, [], bulk=True))

    def _dossier(self, query: str) -> Outcome:
        v = self._vault()
        if not llm.can_spend(v, research_flow.RUN_ESTIMATE_USD):
            return Outcome("Monthly research budget reached.", ok=False)
        place_id, name = research_flow.place_target(v, query)
        path, text = research_flow.dossier_now(v, place_id, name)
        return Outcome(f"{text}\n\n(saved to {path})")

    # -- ripple proposals ---------------------------------------------------------------

    def _sync_proposals(self, v: Vault) -> None:
        """One ripple card per pending proposal file; drop cards answered elsewhere."""
        pending = set(ripple_flow.pending(v))
        for key in self.cards.card_keys():
            if key.startswith("proposal:") and key.removeprefix("proposal:") not in pending:
                self.cards.drop_key(key)
        have = self.cards.card_keys()
        for path in sorted(pending):
            if f"proposal:{path}" not in have:
                self._ripple_card(v, path)

    def _ripple_card(self, v: Vault, path: str) -> Card | None:
        opened = ripple_flow.open_card(v, path)
        if opened is None:
            return None
        text, changes, has_plan = opened
        title, _, detail = text.partition("\n")

        def apply(answer: str) -> Outcome:
            v = self._vault()
            if answer == "skip":
                ripple_flow.mark(v, path, "skipped", [])
                return Outcome("Left everything as is.")
            if answer == "each":
                each, has_draft = ripple_flow.review_each(v, path)
                follow = [self._proposal_card(c) for c in each]
                if has_draft:
                    follow.append(self._adopt_card(path))
                return Outcome("One at a time:", cards=follow)
            return Outcome(ripple_flow.apply_proposal(v, path))

        return self.cards.add(
            "ripple",
            title.removeprefix("⚡ "),
            apply,
            detail=detail.strip(),
            actions=[
                action("all", ripple_flow.apply_label(len(changes), has_plan), "primary"),
                action("each", "One by one"),
                action("skip", "Skip"),
            ],
            key=f"proposal:{path}",
        )

    def _adopt_card(self, path: str) -> Card:
        def apply(answer: str) -> Outcome:
            if answer == "cancel":
                return Outcome("Kept the current plan.")
            plan = ripple_flow.adopt(self._vault(), path, self._today().isoformat())
            return Outcome(f"Current plan is now {plan}." if plan else "No draft found.")

        return self.cards.add(
            "ripple",
            "And the revised plan?",
            apply,
            actions=[action("adopt", "Use it", "primary"), action("cancel", "Keep mine")],
        )

    def _proposal_card(self, proposal: dict[str, Any]) -> Card:
        summary = str(proposal.get("summary") or "Change")

        def apply(answer: str) -> Outcome:
            if answer == "cancel":
                return Outcome(f"Left as is: {summary}")
            try:
                outcome = chat_flow.apply_edit(self._vault(), proposal, self._today())
            except edits.EditError as exc:
                return Outcome(f"Couldn't apply that: {exc}", ok=False)
            except VaultError:
                logger.exception("applying a chat proposal failed")
                return Outcome("Saving failed — check the logs.", ok=False)
            return Outcome(f"Done: {outcome or summary}")

        return self.cards.add("chat_proposal", summary, apply)

    def open_cards(self, req: Request, m: re.Match[str]) -> Reply:
        self._sync_proposals(self._vault())
        return 200, {"cards": self._cards(self.cards.open())}

    def answer_card(self, req: Request, m: re.Match[str]) -> Reply:
        answer = self._json(req).get("action")
        if not isinstance(answer, str) or not answer:
            raise ApiError(400, "missing_field", "'action' is required")
        try:
            outcome = self.cards.answer(m.group(1), answer)
        except UnknownAction as exc:
            raise ApiError(400, "bad_action", "That card has no such action.") from exc
        if outcome is None:
            raise ApiError(410, "expired", "This card expired or was already answered.")
        return 200, {
            "ok": outcome.ok,
            "message": outcome.message,
            "cards": self._cards(outcome.cards),
        }

    # -- chat -----------------------------------------------------------------------------

    def chat_history(self, req: Request, m: re.Match[str]) -> Reply:
        return 200, {"messages": self.chat.messages()}

    def chat_send(self, req: Request, m: re.Match[str]) -> Reply:
        text = _text_field(self._json(req), "text", limit=8000)
        v = self._vault()
        now = self._now()
        try:
            result = chat.respond(v, self.chat.history(), text, now, allow_web=llm.can_spend(v))
        finally:
            llm.flush(v)
        self.chat.record(text, result.text, result.new_history, now)
        cards = [self._proposal_card(p) for p in result.proposals]
        cards += [c for job in result.research_jobs if (c := self._research_job_card(job))]
        return 200, {
            "reply": result.text,
            "cards": self._cards(cards),
            "cost": round(result.cost, 4),
        }

    def chat_reset(self, req: Request, m: re.Match[str]) -> Reply:
        self.chat.reset()
        return 200, {"ok": True}

    # -- daily tools ------------------------------------------------------------------------

    def brief(self, req: Request, m: re.Match[str]) -> Reply:
        last = services.last_brief()
        if last is None:
            return 200, {"date": None, "text": None}
        return 200, {"date": last.date.isoformat(), "text": last.text}

    def make_brief(self, req: Request, m: re.Match[str]) -> Reply:
        v = self._vault()
        today = self._today()
        try:
            text = services.build_brief(v, today)
        finally:
            llm.flush(v)
        self.chat.remember("The app built the morning brief", text)
        return 200, {"date": today.isoformat(), "text": text}

    def surf(self, req: Request, m: re.Match[str]) -> Reply:
        spots = services.surf_data(self._vault())
        empty = None if spots else "No surf spots with a profile around here yet."
        return 200, {"spots": spots, "message": empty}

    def fly(self, req: Request, m: re.Match[str]) -> Reply:
        sites = services.fly_data(self._vault())
        empty = None if sites else "No launch sites with a profile around here yet."
        return 200, {"sites": sites, "message": empty}

    def upcoming(self, req: Request, m: re.Match[str]) -> Reply:
        v = self._vault()
        return 200, {
            "items": services.upcoming_items(v.read_goals(), v.read_itinerary(), self._today())
        }

    def status(self, req: Request, m: re.Match[str]) -> Reply:
        v = self._vault()
        return 200, {"text": services.format_status(v.read_goals(), v.read_itinerary())}

    def usage(self, req: Request, m: re.Match[str]) -> Reply:
        return 200, llm.usage_data(self._vault())

    def site(self, req: Request, m: re.Match[str]) -> Reply:
        if not analytics.configured():
            return 200, {"text": None, "message": "not configured"}
        days = _int(req.arg("days"), 7, high=365)
        try:
            return 200, {"text": analytics.fetch_report(days)}
        except analytics.AnalyticsError as exc:
            logger.warning("site report failed: %s", exc)
            raise ApiError(502, "upstream", "Couldn't reach PostHog.") from exc

    # -- planning -----------------------------------------------------------------------------

    @staticmethod
    def _options(plan: Any) -> list[dict[str, Any]]:
        if plan is None:
            return []
        return [
            {
                "index": i,
                "title": f"{chr(65 + i)}. {o.get('label')}",
                "summary": o.get("summary", ""),
            }
            for i, o in enumerate(plan.options)
        ]

    def plan(self, req: Request, m: re.Match[str]) -> Reply:
        v = self._vault()
        docs = v.list_docs("plans")
        if not docs:
            return 200, {"date": None, "text": None, "options": []}
        path = docs[-1]
        return 200, {
            "date": path.rsplit("/", 1)[-1][:10],
            "text": v.read_text(path),
            "options": self._options(plan_flow.recent_plan(path)),
        }

    def make_plan(self, req: Request, m: re.Match[str]) -> Reply:
        v = self._vault()
        self._budget(v, plan_flow.PLAN_ESTIMATE_USD)
        now = self._now()
        plan, text, path = plan_flow.generate(v, now)
        if not plan.options:
            return 200, {
                "date": now.date().isoformat(),
                "text": plan.situation,
                "options": [],
                "cards": [],
            }
        self.chat.remember("The app made a travel plan", text)

        def apply(answer: str) -> Outcome:
            if answer == "hold":
                return Outcome("Held. Tell the assistant what to change and it'll rework it.")
            try:
                label, applied = plan_flow.choose(
                    self._vault(), plan, path, int(answer), self._today()
                )
            except VaultError:
                logger.exception("applying plan failed")
                return Outcome("Saving failed — check the logs.", ok=False)
            return Outcome(f"Going with {label}. Planned:\n" + "\n".join(f"- {a}" for a in applied))

        card = self.cards.add(
            "plan",
            "Which option?",
            apply,
            actions=[
                action(
                    str(i),
                    f"Go with {chr(65 + i)}: {str(o.get('label'))[:30]}",
                    "primary" if i == plan.recommended else None,
                )
                for i, o in enumerate(plan.options)
            ]
            + [action("hold", "Not yet — I'll adjust in chat")],
        )
        return 200, {
            "date": now.date().isoformat(),
            "text": text,
            "options": self._options(plan),
            "cards": self._cards([card]),
        }

    def ripple(self, req: Request, m: re.Match[str]) -> Reply:
        decision = _text_field(self._json(req), "decision", limit=2000)
        v = self._vault()
        self._budget(v, ripple_flow.RIPPLE_ESTIMATE_USD)
        path, analysis = ripple_flow.trace(v, self._now(), decision)
        text = analysis.replace("**", "")
        self.chat.remember("The app traced a decision", analysis)
        card = self._ripple_card(v, path)
        return 200, {"text": text, "cards": self._cards([card] if card else [])}

    # -- places, trail, wishlist, rankings ---------------------------------------------------

    def trail(self, req: Request, m: re.Match[str]) -> Reply:
        v = self._vault()
        today = self._today()
        arg = req.arg("days", "30")
        if arg != "all":
            arg = str(_int(arg, 30))
        dates = v.list_journal_dates()
        everything = v.read_journal_range(min(dates), today) if dates else {}
        return 200, {
            "text": services.trail_text(v, [arg], today),
            "stays": dashboard.stay_rows(v.read_location(), everything, today),
        }

    def places(self, req: Request, m: re.Match[str]) -> Reply:
        query = req.arg("q").strip()[:100]
        if not query:
            return 200, {"results": []}
        index = PlaceIndex(self._vault().read_places())
        return 200, {
            "results": [
                {
                    "id": pid,
                    "name": index.by_id[pid].get("name"),
                    "kind": index.by_id[pid].get("kind"),
                    "path": index.path_name(pid),
                }
                for pid in services.find_places(index, query)[:20]
            ]
        }

    def place(self, req: Request, m: re.Match[str]) -> Reply:
        v = self._vault()
        index = PlaceIndex(v.read_places())
        node = index.get(m.group(1))
        if node is None:
            raise ApiError(404, "not_found", "No such place.")
        children = index.descendants(node["id"])
        return 200, {
            "id": node["id"],
            "name": node.get("name"),
            "kind": node.get("kind"),
            "path": index.path_name(node["id"]),
            "description": plain(node.get("description")),
            "facts": plain(node.get("facts") or {}),
            "checklist": plain(node.get("checklist") or []),
            "my_notes": plain(node.get("my_notes") or []),
            "children": [
                {"id": c["id"], "name": c.get("name"), "kind": c.get("kind")}
                for c in children[:100]
            ],
            "card_text": services.place_card_text(v, index, node, self._today()),
        }

    def wishlist(self, req: Request, m: re.Match[str]) -> Reply:
        entries = wishlist_flow.active_entries(self._vault().read_itinerary())
        return 200, {
            "entries": [
                {
                    "id": e.get("id"),
                    "place": e.get("place"),
                    "status": e.get("status"),
                    "type": e.get("type", "soft"),
                    "date": itinerary.current_date(e),
                    "research": e.get("research", "none"),
                    "why": plain(e.get("why") or []),
                }
                for e in entries
            ]
        }

    def want(self, req: Request, m: re.Match[str]) -> Reply:
        query = _text_field(self._json(req), "place", limit=200)
        entry, created = wishlist_flow.add_wish(self._vault(), query, self._today())
        entry_id = str(entry["id"])

        def apply(answer: str) -> Outcome:
            if answer == "later":
                return Outcome("Queued — tomorrow's morning routine will research it.")
            v = self._vault()
            target = wishlist_flow.research_target(v, entry_id)
            if isinstance(target, str):
                return Outcome(target, ok=False)
            place_id, name = target
            path, text = wishlist_flow.research_entry(v, entry_id, place_id, name)
            return Outcome(f"{text}\n\n(saved to {path})")

        card = self.cards.add(
            "want",
            wishlist_flow.want_message(entry, created),
            apply,
            actions=[
                action("now", "Research now", "primary"),
                action("later", "Leave it for the morning routine"),
            ],
        )
        return 200, {"cards": self._cards([card])}

    def rankings(self, req: Request, m: re.Match[str]) -> Reply:
        v = self._vault()
        current = v.read_yaml(rank_flow.RANKINGS_FILE, {})
        index = PlaceIndex(v.read_places())
        wanted = req.arg("category")
        categories = list(rankings.CATEGORIES)
        lists: dict[str, list[dict[str, Any]]] = {}
        for cat in [wanted] if wanted in rankings.CATEGORIES else categories:
            tiers = [
                {
                    "tier": tier,
                    "items": [
                        str((index.get(pid) or {}).get("name", pid))
                        for pid in rankings.tier_list(current, cat, tier)
                    ],
                }
                for tier in rankings.TIERS
            ]
            if any(t["items"] for t in tiers):
                lists[cat] = [t for t in tiers if t["items"]]
        return 200, {"categories": categories, "lists": lists}

    def research(self, req: Request, m: re.Match[str]) -> Reply:
        data = self._json(req)
        if data.get("place"):
            outcome = self._dossier(_text_field(data, "place", limit=200))
            return 200, {"message": outcome.message, "cards": []}
        kind = data.get("kind")
        v = self._vault()
        if kind == "explore":
            outcome = self._explore()
            return 200, {"message": outcome.message, "cards": self._cards(outcome.cards)}
        if kind == "trip":
            job = research_flow.plan_trip(v, self._today())
            if isinstance(job, str):
                return 200, {"message": job, "cards": []}
            headline, findings = research_flow.run_trip(v, job)
            return 200, {
                "message": headline,
                "cards": self._cards(self._place_cards(findings, job.days)),
            }
        if kind == "backfill":
            n = _int(str(data.get("arg") or ""), 30, high=365)
            today = self._today()
            days = [today - timedelta(days=i) for i in range(n)]
            said, found = research_flow.lookup_mentions(v, days, quiet=False)
            cards = self._place_cards(found, days) if found else []
            return 200, {"message": said or "Nothing to look up.", "cards": self._cards(cards)}
        raise ApiError(400, "bad_kind", "Send 'place', or 'kind': explore | trip | backfill.")

    # -- review, disciplines, undo -----------------------------------------------------------

    def review(self, req: Request, m: re.Match[str]) -> Reply:
        v = self._vault()
        docs = v.list_docs("reviews")
        if not docs:
            return 200, {"week": None, "text": None}
        return 200, {
            "week": docs[-1].rsplit("/", 1)[-1].removesuffix(".md"),
            "text": v.read_text(docs[-1]),
        }

    def make_review(self, req: Request, m: re.Match[str]) -> Reply:
        written = daily.write_review(self._vault(), self._today())
        if written is None:
            raise ApiError(404, "nothing_to_review", "No journal entries this week to review.")
        label, _start, _end, text = written
        self.chat.remember("The app wrote the weekly review", text)
        return 200, {"week": label, "text": text}

    def disciplines(self, req: Request, m: re.Match[str]) -> Reply:
        v = self._vault()
        days = _int(req.arg("days"), disciplines.RECENT_DAYS)
        graph, recent, all_time = disciplines.snapshot(v, self._today(), days)
        if not graph:
            return 200, {"days": days, "nodes": [], "slots": {}}
        return 200, {
            "days": days,
            "nodes": disciplines.graph_data(graph, v.read_goals(), recent, all_time),
            "slots": dashboard.discipline_slots(graph),
        }

    def changes(self, req: Request, m: re.Match[str]) -> Reply:
        return 200, {
            "changes": [
                {"sha": sha, "subject": subject} for sha, subject in self._vault().recent_changes()
            ]
        }

    def revert(self, req: Request, m: re.Match[str]) -> Reply:
        sha = m.group(1)
        v = self._vault()
        # Only the recent changes /undo offers, never an arbitrary commit.
        if sha not in {s for s, _ in v.recent_changes()}:
            raise ApiError(404, "unknown_change", "That isn't one of the recent changes.")
        try:
            v.revert(sha)
        except VaultError as exc:
            raise ApiError(409, "conflict", f"Couldn't undo that: {exc}") from exc
        return 200, {"ok": True, "message": f"Undone ({sha})."}


def _spawn_thread(fn: Callable[[], None]) -> None:
    def run() -> None:
        try:
            fn()
        except Exception:
            logger.exception("background research failed")

    threading.Thread(target=run, daemon=True).start()
