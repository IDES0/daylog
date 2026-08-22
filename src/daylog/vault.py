"""All filesystem and git access for the vault.

This is the only module in daylog that touches the filesystem or runs git
commands. Every other module works with plain data in memory and hands it
to `Vault` to persist.
"""

from __future__ import annotations

import io
import logging
import subprocess
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
    "goal_progress",
    "skipped",
    "mood",
    "open_questions",
)

# Frontmatter fields that get extended (list) or overwritten (scalar) when a
# second entry lands on a day that already has one. Anything not listed here
# is treated as a scalar (new value wins) when merging.
_LIST_FIELDS_EXTEND = ("activities", "goal_progress")
_LIST_FIELDS_DEDUPE = ("skipped", "open_questions")


class VaultError(RuntimeError):
    """Raised when a vault write (file or git commit) fails.

    Push failures are not fatal (see `Vault._push`) since the commit that
    matters has already landed locally; everything else must raise, since a
    silent failure here means a lost journal entry.
    """


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

        self._commit(self.goals_path, commit_message)
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

        self._commit(self.itinerary_path, commit_message)
        self._push()
        return self.itinerary_path

    @property
    def places_path(self) -> Path:
        return self.path / "places.yaml"

    def read_places(self) -> Any:
        """Load places.yaml — hand-curated destination knowledge, read-only for now."""
        if not self.places_path.exists():
            return []
        return _yaml().load(self.places_path.read_text(encoding="utf-8"))

    @property
    def location_path(self) -> Path:
        return self.path / "location.yaml"

    def read_location(self) -> Any:
        """Load location.yaml — retrospective record of where the user was, read-only."""
        if not self.location_path.exists():
            return []
        return _yaml().load(self.location_path.read_text(encoding="utf-8"))

    @property
    def profile_path(self) -> Path:
        return self.path / "profile.yaml"

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
            transcript_body = f"{existing.transcript}\n\n### {heading}\n\n{transcript}"
            summary_body = f"{existing.summary}\n\n### {heading}\n\n{summary}"
            commit_message = f"journal: {entry_date.isoformat()} (+entry)"

        body = self._render_journal(merged_frontmatter, transcript_body, summary_body)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(body, encoding="utf-8")

        self._commit(target, commit_message)
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

    # -- git -----------------------------------------------------------

    def _run_git(self, *args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            ["git", "-C", str(self.path), *args],
            capture_output=True,
            text=True,
            check=False,
        )

    def _commit(self, changed_path: Path, message: str) -> None:
        add = self._run_git("add", str(changed_path))
        if add.returncode != 0:
            raise VaultError(f"git add failed: {add.stderr.strip()}")

        commit = self._run_git("commit", "-m", message)
        if commit.returncode != 0:
            raise VaultError(f"git commit failed: {commit.stderr.strip()}")

    def _push(self) -> bool:
        push = self._run_git("push")
        if push.returncode != 0:
            logger.warning(
                "git push failed, commit kept locally and will retry next write: %s",
                push.stderr.strip(),
            )
            return False
        return True
