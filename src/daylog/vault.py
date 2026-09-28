"""All filesystem and git access for the vault.

This is the only module in daylog that touches the filesystem or runs git
commands. Every other module works with plain data in memory and hands it
to `Vault` to persist.
"""

from __future__ import annotations

import io
import logging
import subprocess
import time
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path
from typing import Any

from ruamel.yaml import YAML

logger = logging.getLogger(__name__)

_FRONTMATTER_KEY_ORDER = (
    "date",
    "location",
    "activities",
    "meals",
    "felt",
    "goal_progress",
    "skipped",
    "mood",
    "open_questions",
    "reconciled",
)

# How often `sync()` may actually hit the remote. Every handler calls it,
# so this bounds git traffic while still picking up a change pushed from
# elsewhere (the research routine, a manual fix) within a minute.
_SYNC_INTERVAL_SECONDS = 60

# Frontmatter fields that get extended (list) or overwritten (scalar) when a
# second entry lands on a day that already has one. Anything not listed here
# is treated as a scalar (new value wins) when merging.
_LIST_FIELDS_EXTEND = ("activities", "meals", "felt", "goal_progress")
_LIST_FIELDS_DEDUPE = ("skipped", "open_questions")


class VaultError(RuntimeError):
    """Raised when a vault write (file or git commit) fails.

    Push failures are not fatal (see `Vault._push`) since the commit that
    matters has already landed locally; everything else must raise, since a
    silent failure here means a lost journal entry.
    """


class CorrectionConflictError(VaultError):
    """A correction's target no longer matches what was captured when it was
    proposed — e.g. a later message changed the same list before the user
    confirmed. Refuse rather than risk removing the wrong item."""


@dataclass
class JournalEntry:
    date: date
    frontmatter: dict[str, Any]
    transcript: str
    summary: str
    raw: str = field(repr=False)


def _yaml() -> YAML:
    yaml = YAML()
    yaml.default_flow_style = False
    yaml.indent(mapping=2, sequence=4, offset=2)
    return yaml


def _describe(result: subprocess.CompletedProcess[str]) -> str:
    """Git's failure reason isn't always on stderr — e.g. a no-op `git commit`
    ("nothing to commit, working tree clean") prints to stdout and leaves
    stderr empty, which previously made the resulting VaultError message
    empty and undiagnosable from logs. Report whichever stream has content."""
    reason = result.stderr.strip() or result.stdout.strip()
    return reason or f"exit code {result.returncode}, no output"


def _merge_frontmatter(
    existing: dict[str, Any], new: dict[str, Any], entry_date: date
) -> dict[str, Any]:
    merged = dict(existing)
    merged["date"] = entry_date
    for key, value in new.items():
        if value is None:
            continue
        if key in _LIST_FIELDS_EXTEND:
            merged[key] = list(existing.get(key) or []) + list(value)
        elif key in _LIST_FIELDS_DEDUPE:
            combined = list(existing.get(key) or [])
            for item in value:
                if item not in combined:
                    combined.append(item)
            merged[key] = combined
        else:
            merged[key] = value
    return merged


class Vault:
    def __init__(self, path: Path) -> None:
        self.path = path

    # -- paths -----------------------------------------------------------

    def journal_path(self, entry_date: date) -> Path:
        return self.path / "journal" / f"{entry_date.isoformat()}.md"

    def list_journal_dates(self) -> list[date]:
        """All dates with a journal entry, oldest first.

        Skips anything whose filename isn't a plain `YYYY-MM-DD.md` (e.g. a
        stray `.bak` or manually-named test file) rather than raising.
        """
        journal_dir = self.path / "journal"
        if not journal_dir.exists():
            return []
        dates = []
        for entry_path in journal_dir.glob("*.md"):
            try:
                dates.append(date.fromisoformat(entry_path.stem))
            except ValueError:
                continue
        return sorted(dates)

    @property
    def goals_path(self) -> Path:
        return self.path / "goals.yaml"

    # -- goals -----------------------------------------------------------

    def read_goals(self) -> Any:
        """Load goals.yaml via ruamel's round-trip loader.

        Returns the live ruamel structure (a CommentedSeq of CommentedMaps),
        not a plain dict/list — mutate it in place and pass the same object
        to write_goals. Rebuilding a plain structure and dumping that would
        silently drop the user's hand-written comments (goals.yaml is
        explicitly hand-edited — see its own header comment).
        """
        if not self.goals_path.exists():
            return []
        return _yaml().load(self.goals_path.read_text(encoding="utf-8"))

    def write_goals(self, goals: Any, commit_message: str) -> Path:
        buf = io.StringIO()
        _yaml().dump(goals, buf)
        self.goals_path.write_text(buf.getvalue(), encoding="utf-8")

        self._commit(self.goals_path, message=commit_message)
        self._push()
        return self.goals_path

    @property
    def itinerary_path(self) -> Path:
        return self.path / "itinerary.yaml"

    # -- itinerary -----------------------------------------------------------

    def read_itinerary(self) -> Any:
        """Load itinerary.yaml via ruamel's round-trip loader — see read_goals."""
        if not self.itinerary_path.exists():
            return []
        return _yaml().load(self.itinerary_path.read_text(encoding="utf-8"))

    def write_itinerary(self, itinerary: Any, commit_message: str) -> Path:
        buf = io.StringIO()
        _yaml().dump(itinerary, buf)
        self.itinerary_path.write_text(buf.getvalue(), encoding="utf-8")

        self._commit(self.itinerary_path, message=commit_message)
        self._push()
        return self.itinerary_path

    # -- places -----------------------------------------------------------

    @property
    def places_dir(self) -> Path:
        return self.path / "places"

    def read_place_files(self) -> dict[str, Any]:
        """places/<file>.yaml -> its live ruamel list of nodes, keyed by file stem.

        Mutate a node in place and hand the same dict back to
        `write_place_files` — see read_goals for why the live structure
        matters (hand-written comments survive).
        """
        if not self.places_dir.exists():
            return {}
        files: dict[str, Any] = {}
        for path in sorted(self.places_dir.glob("*.yaml")):
            files[path.stem] = _yaml().load(path.read_text(encoding="utf-8")) or []
        return files

    def read_places(self) -> list[Any]:
        """Every place node across all places/ files, as one flat list."""
        return [node for nodes in self.read_place_files().values() for node in nodes]

    def write_place_files(
        self, files: dict[str, Any], changed: set[str], commit_message: str
    ) -> list[Path]:
        """Write the named files from `files` (all of them must be keys) and commit once."""
        self.places_dir.mkdir(parents=True, exist_ok=True)
        paths = []
        for stem in sorted(changed):
            path = self.places_dir / f"{stem}.yaml"
            self._dump(files[stem], path)
            paths.append(path)
        self._commit(*paths, message=commit_message)
        self._push()
        return paths

    # -- generic yaml docs (rankings, usage, ...) ----------------------------

    def read_yaml(self, name: str, default: Any) -> Any:
        path = self.path / name
        if not path.exists():
            return default
        loaded = _yaml().load(path.read_text(encoding="utf-8"))
        return default if loaded is None else loaded

    def write_yaml(self, name: str, data: Any, commit_message: str) -> Path:
        path = self.path / name
        path.parent.mkdir(parents=True, exist_ok=True)
        self._dump(data, path)
        self._commit(path, message=commit_message)
        self._push()
        return path

    # -- markdown docs (research, plans, reviews) ------------------------------

    def read_text(self, name: str) -> str | None:
        path = self.path / name
        return path.read_text(encoding="utf-8") if path.exists() else None

    def list_docs(self, folder: str) -> list[str]:
        """Relative paths of markdown files under `folder`, newest name last."""
        base = self.path / folder
        if not base.exists():
            return []
        return sorted(str(p.relative_to(self.path)) for p in base.glob("*.md"))

    def write_text(self, name: str, text: str, commit_message: str) -> Path:
        path = self.path / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        self._commit(path, message=commit_message)
        self._push()
        return path

    def _dump(self, data: Any, path: Path) -> None:
        buf = io.StringIO()
        _yaml().dump(data, buf)
        path.write_text(buf.getvalue(), encoding="utf-8")

    @property
    def location_path(self) -> Path:
        return self.path / "location.yaml"

    def read_location(self) -> Any:
        """Load location.yaml — retrospective record of where the user was, read-only."""
        if not self.location_path.exists():
            return []
        return _yaml().load(self.location_path.read_text(encoding="utf-8"))

    def write_location(
        self,
        place: str,
        lat: float | None,
        lon: float | None,
        on: date,
        commit_message: str,
        place_id: str | None = None,
        mode: str | None = None,
    ) -> Path:
        """Close whichever entry is currently open (`to: null`) as of `on`, and open a new one.

        `lat`/`lon` are optional — extraction only supplies them when
        confident (see prompts/extract.md); a place with neither still
        updates *where* the brief thinks the user is, it just won't have a
        marine/wind forecast until coordinates are known.
        """
        locations = self.read_location()
        for entry in locations:
            if entry.get("to") is None:
                entry["to"] = on

        new_entry: dict[str, Any] = {"place": place}
        if place_id:
            new_entry["place_id"] = place_id
        if mode and mode != "stay":
            new_entry["mode"] = mode
        if lat is not None:
            new_entry["lat"] = lat
        if lon is not None:
            new_entry["lon"] = lon
        new_entry["from"] = on
        new_entry["to"] = None
        locations.append(new_entry)

        buf = io.StringIO()
        _yaml().dump(locations, buf)
        self.location_path.write_text(buf.getvalue(), encoding="utf-8")

        self._commit(self.location_path, message=commit_message)
        self._push()
        return self.location_path

    @property
    def profile_path(self) -> Path:
        return self.path / "profile.yaml"

    def read_principles(self) -> str:
        """profile.md — the user's own operating principles (what drives them, how
        their motivation works, their rules). Private to the vault; '' if absent."""
        return self.read_text("profile.md") or ""

    def write_profile(self, profile: Any, commit_message: str) -> Path:
        return self.write_yaml("profile.yaml", profile, commit_message)

    def read_profile(self) -> Any:
        """Load profile.yaml — hand-curated durable preferences, read-only for now."""
        if not self.profile_path.exists():
            return {}
        return _yaml().load(self.profile_path.read_text(encoding="utf-8"))

    # -- journal -----------------------------------------------------------

    def write_journal_entry(
        self,
        entry_time: datetime,
        frontmatter: dict[str, Any],
        transcript: str,
        summary: str,
    ) -> Path:
        """Write or append to journal/YYYY-MM-DD.md, commit it, and push.

        If an entry already exists for `entry_time`'s date, the new
        transcript/summary are appended as a timestamped subsection and
        list-valued frontmatter fields (activities, goal_progress, ...) are
        merged rather than overwritten — a second voice note in a day adds
        to that day, it doesn't replace it.

        A push failure is logged and swallowed: the commit lands locally, and
        the next successful push carries it along.
        """
        entry_date = entry_time.date()
        heading = entry_time.strftime("%H:%M")
        target = self.journal_path(entry_date)

        existing = self.read_journal_entry(entry_date)
        if existing is None:
            merged_frontmatter = {"date": entry_date, **frontmatter}
            transcript_body = f"### {heading}\n\n{transcript}"
            summary_body = f"### {heading}\n\n{summary}"
            commit_message = f"journal: {entry_date.isoformat()}"
        else:
            merged_frontmatter = _merge_frontmatter(existing.frontmatter, frontmatter, entry_date)
            # A new note after the end-of-day reconcile means the day needs
            # reconciling again.
            merged_frontmatter.pop("reconciled", None)
            transcript_body = f"{existing.transcript}\n\n### {heading}\n\n{transcript}"
            summary_body = f"{existing.summary}\n\n### {heading}\n\n{summary}"
            commit_message = f"journal: {entry_date.isoformat()} (+entry)"

        body = self._render_journal(merged_frontmatter, transcript_body, summary_body)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(body, encoding="utf-8")

        self._commit(target, message=commit_message)
        self._push()
        return target

    def remove_journal_item(
        self,
        entry_date: date,
        field: str,
        index: int,
        expected_item: Any,
        commit_message: str,
    ) -> Path:
        """Remove one item from a journal frontmatter list, preserving everything else.

        `expected_item` must equal what's currently at `index` — if the
        entry changed since the correction was proposed (e.g. a later
        message extended the same list), this raises CorrectionConflictError
        rather than risk deleting the wrong thing.
        """
        entry = self.read_journal_entry(entry_date)
        if entry is None:
            raise VaultError(f"no journal entry for {entry_date.isoformat()} to correct")

        items = entry.frontmatter.get(field)
        if not items or not (0 <= index < len(items)) or items[index] != expected_item:
            raise CorrectionConflictError(
                f"correction target no longer matches: {field}[{index}] on {entry_date.isoformat()}"
            )

        del items[index]
        target = self.journal_path(entry_date)
        target.write_text(
            self._render_journal(entry.frontmatter, entry.transcript, entry.summary),
            encoding="utf-8",
        )
        self._commit(target, message=commit_message)
        self._push()
        return target

    def read_journal_entry(self, entry_date: date) -> JournalEntry | None:
        target = self.journal_path(entry_date)
        if not target.exists():
            return None
        raw = target.read_text(encoding="utf-8")
        frontmatter, transcript, summary = self._parse_journal(raw)
        return JournalEntry(
            date=entry_date,
            frontmatter=frontmatter,
            transcript=transcript,
            summary=summary,
            raw=raw,
        )

    def read_journal_range(self, start: date, end: date) -> dict[date, JournalEntry]:
        """Every entry dated start..end inclusive. A file that fails to parse is
        skipped and logged, never fatal — one bad day must not hide the rest."""
        out: dict[date, JournalEntry] = {}
        for entry_date in self.list_journal_dates():
            if not (start <= entry_date <= end):
                continue
            try:
                entry = self.read_journal_entry(entry_date)
            except Exception:
                logger.exception("skipping unparseable journal entry %s", entry_date)
                continue
            if entry is not None:
                out[entry_date] = entry
        return out

    def rewrite_journal_entry(
        self,
        entry_date: date,
        frontmatter: dict[str, Any],
        commit_message: str,
        summary: str | None = None,
    ) -> Path:
        """Replace an existing entry's frontmatter (and optionally its summary).

        The transcript is never touched — it's the user's own words and the
        source everything else is re-derivable from. Used by place linking
        and the end-of-day reconcile, which rebuild the derived parts.
        """
        entry = self.read_journal_entry(entry_date)
        if entry is None:
            raise VaultError(f"no journal entry for {entry_date.isoformat()} to rewrite")
        target = self.journal_path(entry_date)
        target.write_text(
            self._render_journal(
                {**frontmatter, "date": entry_date},
                entry.transcript,
                entry.summary if summary is None else summary,
            ),
            encoding="utf-8",
        )
        self._commit(target, message=commit_message)
        self._push()
        return target

    def _render_journal(self, frontmatter: dict[str, Any], transcript: str, summary: str) -> str:
        ordered = {
            key: frontmatter[key]
            for key in _FRONTMATTER_KEY_ORDER
            if key in frontmatter and frontmatter[key] is not None
        }
        extra_keys = set(frontmatter) - set(ordered)
        for key in extra_keys:
            if frontmatter[key] is not None:
                ordered[key] = frontmatter[key]

        yaml = _yaml()
        buf = io.StringIO()
        yaml.dump(ordered, buf)

        return (
            f"---\n{buf.getvalue()}---\n\n"
            f"## Transcript\n\n{transcript}\n\n"
            f"## Summary\n\n{summary}\n"
        )

    def _parse_journal(self, raw: str) -> tuple[dict[str, Any], str, str]:
        _, fm_text, rest = raw.split("---", 2)
        yaml = _yaml()
        frontmatter = yaml.load(fm_text) or {}

        transcript = ""
        summary = ""
        section = None
        for line in rest.splitlines():
            if line.strip() == "## Transcript":
                section = "transcript"
                continue
            if line.strip() == "## Summary":
                section = "summary"
                continue
            if section == "transcript":
                transcript += line + "\n"
            elif section == "summary":
                summary += line + "\n"

        return dict(frontmatter), transcript.strip(), summary.strip()

    # -- history / undo ----------------------------------------------------

    def recent_changes(self, limit: int = 8) -> list[tuple[str, str]]:
        """(sha, subject) of recent commits worth undoing — newest first, skipping
        cost bookkeeping and merges."""
        log = self._run_git("log", "--no-merges", "-n", str(limit * 4), "--format=%h%x09%s")
        out: list[tuple[str, str]] = []
        for line in log.stdout.splitlines():
            sha, _, subject = line.partition("\t")
            if subject.startswith("usage:"):
                continue
            out.append((sha, subject))
            if len(out) >= limit:
                break
        return out

    def revert(self, sha: str) -> None:
        """Undo one commit with a new commit. A conflict (a later change touched the
        same lines) aborts cleanly and raises — nothing half-reverted is left."""
        result = self._run_git("revert", "--no-edit", sha)
        if result.returncode != 0:
            self._run_git("revert", "--abort")
            raise VaultError(f"can't undo {sha} cleanly: {_describe(result)}")
        self._push()

    # -- git -----------------------------------------------------------

    _last_sync: float = 0.0

    def sync(self, force: bool = False) -> None:
        """Catch up with the remote, at most once a minute unless `force`.

        Other writers push to the vault too (the scheduled research routine,
        a manual fix). Before this, the bot only saw their changes after a
        restart. Fast-forward when possible; with local unpushed commits,
        rebase them on top. A failure is logged and ignored — stale data
        for a minute is better than a failed handler.
        """
        now = time.monotonic()
        if not force and now - Vault._last_sync < _SYNC_INTERVAL_SECONDS:
            return
        Vault._last_sync = now
        fetch = self._run_git("fetch", "origin")
        if fetch.returncode != 0:
            logger.warning("vault sync: fetch failed: %s", _describe(fetch))
            return
        merge = self._run_git("merge", "--ff-only", "origin/main")
        if merge.returncode == 0:
            return
        if self._reconcile_with_remote():
            self._push()

    def _run_git(self, *args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            ["git", "-C", str(self.path), *args],
            capture_output=True,
            text=True,
            check=False,
        )

    def _commit(self, *changed_paths: Path, message: str) -> None:
        # git -C resolves pathspecs against the vault dir, so a relative
        # VAULT_PATH ("../daylog-vault") would otherwise be applied twice.
        root = self.path.resolve()
        relative = [str(p.resolve().relative_to(root)) for p in changed_paths]
        add = self._run_git("add", "--", *relative)
        if add.returncode != 0:
            raise VaultError(f"git add failed: {_describe(add)}")

        commit = self._run_git("commit", "-m", message)
        if commit.returncode != 0:
            reason = _describe(commit)
            if "nothing to commit" in reason:
                # The content we just wrote is byte-identical to what's
                # already committed. In normal operation every write changes
                # something first, so this means the desired end state was
                # already reached — most likely a duplicate write (e.g.
                # Telegram redelivering an update after a restart re-applies
                # an already-saved change). That's not a failure to raise.
                logger.info("nothing to commit for %s — write was a no-op", changed_paths)
                return
            raise VaultError(f"git commit failed: {reason}")

    def _push(self) -> bool:
        push = self._run_git("push")
        if push.returncode == 0:
            return True

        logger.warning(
            "git push failed, attempting to reconcile with the remote: %s", _describe(push)
        )

        # A rejected push isn't always transient network flakiness — the
        # remote can have moved ahead of what this clone last knew about
        # (e.g. a commit landing from a different clone of the same repo),
        # in which case a bare retry would fail identically forever. Rebase
        # local commits on top of the remote and retry once.
        if not self._reconcile_with_remote():
            logger.warning("reconciliation failed, commit kept locally and will retry next write")
            return False

        retry = self._run_git("push")
        if retry.returncode != 0:
            logger.warning(
                "git push still failed after reconciling, commit kept locally "
                "and will retry next write: %s",
                _describe(retry),
            )
            return False
        return True

    def _reconcile_with_remote(self) -> bool:
        """Fetch and rebase local commits onto the remote's current state.

        If the rebase doesn't apply cleanly (a real conflict, not just a
        clean fast-forward-able divergence), abort it so the working tree
        is left exactly as it was rather than mid-conflict — the commit
        stays local and safe, just not pushed yet.
        """
        fetch = self._run_git("fetch", "origin")
        if fetch.returncode != 0:
            logger.warning("git fetch failed during reconciliation: %s", _describe(fetch))
            return False

        rebase = self._run_git("rebase", "origin/main")
        if rebase.returncode == 0:
            return True

        logger.warning("git rebase failed during reconciliation, aborting: %s", _describe(rebase))
        self._run_git("rebase", "--abort")
        return False
