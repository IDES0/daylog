"""Notes are logged in the background, so the app can close once they're uploaded.

A voice note took 30–60 s to transcribe and log while the app held the request
open. Closing the app cut the request: the server logged the note anyway, the
app said it failed, and Retry uploaded it again — on 2026-10-22 one note was
logged three times. Now POST /api/voice and /api/log answer 202 with a job id
as soon as the upload has arrived; the app polls /api/jobs/{id}, or catches up
from /api/jobs when it's opened again.

Each note's Idempotency-Key (one per recording or typed note, reused by Retry;
the body's hash when absent) maps to one job: sending it again returns the
same job instead of starting another. Only a failed job may run again.

Notes survive a restart (since 2026-10-24). Before the 202, the note (its text,
or its audio and type) and its job record go to the spool (`Vault.spool`, on
the vault's volume, never committed). When a job ends, its record keeps the
status and result (a failed one keeps the transcript, for Try again) and the
audio is dropped. On start, `load` reads the records back: finished jobs answer
/api/jobs as before, and each note still waiting is run again under the same
job id and key. A note that was mid-way when the server died can run a second
time; one already started MAX_ATTEMPTS times is failed instead, so a note that
takes the server down can't do it on every boot. Records are kept for a day.
"""

from __future__ import annotations

import hashlib
import logging
import threading
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from functools import partial
from typing import Any

from daylog.vault import Spool

logger = logging.getLogger(__name__)

TTL_S = 24 * 3600
MAX_ATTEMPTS = 3

Reply = tuple[int, dict[str, Any]]


def key_for(header: str | None, kind: str, body: bytes, extra: str = "") -> str:
    """The client's key when it sent one, else a hash of what was sent."""
    if header and header.strip():
        return f"{kind}:key:{header.strip()[:100]}"
    return f"{kind}:sha:{hashlib.sha256(body + extra.encode()).hexdigest()}"


@dataclass
class Job:
    id: str
    key: str
    kind: str
    status: str = "processing"  # processing | done | failed
    reply: Reply | None = None
    started: float = field(default_factory=time.time)
    finished: float | None = None
    # What's needed to log the note again after a restart (its text, or its
    # audio's type, and the date); cleared once the job ends.
    note: dict[str, Any] | None = None
    attempts: int = 0

    def view(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "id": self.id,
            "kind": self.kind,
            "status": self.status,
            "started": self.started,
        }
        if self.reply is not None:
            out["code"], out["result"] = self.reply
        return out

    def record(self) -> dict[str, Any]:
        """What the spool keeps: small, never the audio."""
        out = self.view()
        out.update(key=self.key, finished=self.finished, attempts=self.attempts)
        if self.note is not None:
            out["note"] = self.note
        return out

    @classmethod
    def from_record(cls, r: dict[str, Any]) -> Job:
        reply = (int(r["code"]), dict(r["result"])) if "code" in r else None
        finished = r.get("finished")
        return cls(
            id=str(r["id"]),
            key=str(r["key"]),
            kind=str(r["kind"]),
            status=str(r.get("status", "processing")),
            reply=reply,
            started=float(r["started"]),
            finished=float(finished) if finished is not None else None,
            note=r.get("note"),
            attempts=int(r.get("attempts") or 0),
        )


class Jobs:
    def __init__(
        self,
        run: Callable[[Callable[[], None]], None],
        spool: Callable[[], Spool] | None = None,
    ) -> None:
        self._run = run
        self._spool_of = spool
        self._spool: Spool | None = None
        self._lock = threading.Lock()
        self._by_id: dict[str, Job] = {}
        self._by_key: dict[str, str] = {}

    def submit(
        self,
        key: str,
        kind: str,
        work: Callable[[], Reply],
        note: dict[str, Any] | None = None,
        audio: bytes | None = None,
    ) -> tuple[Job, bool]:
        """(job, new): start `work` unless this note already has a live or finished job.

        `note` (and a voice note's `audio`) are spooled before this returns,
        so `load` can log the note again after a restart."""
        with self._lock:
            self._prune()
            existing = self._by_id.get(self._by_key.get(key, ""))
            if existing is not None and existing.status != "failed":
                return existing, False
            job = Job(id=uuid.uuid4().hex[:16], key=key, kind=kind, note=note)
            self._by_id[job.id] = job
            self._by_key[key] = job.id
            if audio is not None:
                self._store(lambda s: s.save_audio(job.id, audio))
        self._start(job, work)
        return job, True

    def load(self) -> list[Job]:
        """Read the spool back after a restart. Finished jobs are known again;
        the notes still waiting are returned for the caller to `resume`."""
        spool = self._spool_now()
        if spool is None:
            return []
        waiting: list[Job] = []
        cutoff = time.time() - TTL_S
        jobs: list[Job] = []
        for record in spool.jobs():
            try:
                jobs.append(Job.from_record(record))
            except (KeyError, TypeError, ValueError):
                logger.warning("spool: bad job record %s, skipped", record.get("id"))
        with self._lock:
            for job in sorted(jobs, key=lambda j: j.started):
                if job.id in self._by_id:
                    continue
                if job.started < cutoff:
                    self._store(partial(Spool.drop_job, job_id=job.id))
                    continue
                self._by_id[job.id] = job
                # A key maps to its latest job, unless that one failed and an
                # earlier one didn't (only a failed job is ever run again).
                current = self._by_id.get(self._by_key.get(job.key, ""))
                if current is None or current.status == "failed" or job.status != "failed":
                    self._by_key[job.key] = job.id
                if job.status == "processing":
                    waiting.append(job)
        return waiting

    def resume(self, job: Job, work: Callable[[], Reply]) -> None:
        """Run a note found waiting by `load` again, under its old id and key."""
        if job.attempts >= MAX_ATTEMPTS:
            logger.warning("note %s was started %d times; giving up", job.id, job.attempts)
            body: dict[str, Any] = {
                "error": "gave_up",
                "message": "This note stopped the server each time it ran, so it wasn't logged.",
            }
            if job.note and job.note.get("text"):
                body["text"] = job.note["text"]
            self._finish(job, (500, body))
            return
        logger.info("resuming note %s (%s) after a restart", job.id, job.kind)
        self._start(job, work)

    def fail(self, job: Job, reply: Reply) -> None:
        """End a waiting note that can't run again (e.g. its audio is gone)."""
        self._finish(job, reply)

    def get(self, job_id: str) -> Job | None:
        with self._lock:
            return self._by_id.get(job_id)

    def recent(self) -> list[Job]:
        with self._lock:
            self._prune()
            return sorted(self._by_id.values(), key=lambda j: j.started, reverse=True)

    # -- inside ---------------------------------------------------------------

    def _start(self, job: Job, work: Callable[[], Reply]) -> None:
        with self._lock:
            job.attempts += 1
            self._store(lambda s: s.save_job(job.id, job.record()))

        def go() -> None:
            try:
                reply = work()
            except Exception as exc:  # the handler already logged it
                reply = (500, {"error": "failed", "message": str(exc) or "Logging failed."})
            self._finish(job, reply)

        self._run(go)

    def _finish(self, job: Job, reply: Reply) -> None:
        with self._lock:
            job.reply = reply
            job.status = "done" if reply[0] < 400 else "failed"
            job.finished = time.time()
            job.note = None
            self._store(lambda s: s.save_job(job.id, job.record()))
            self._store(lambda s: s.drop_audio(job.id))

    def _spool_now(self) -> Spool | None:
        if self._spool is None and self._spool_of is not None:
            try:
                self._spool = self._spool_of()
            except Exception:
                logger.exception("spool unavailable; notes are kept in memory only")
                self._spool_of = None
        return self._spool

    def _store(self, op: Callable[[Spool], None]) -> None:
        """A spool write. A failure is logged, not raised: the note is still
        logged from memory, it just wouldn't survive a restart."""
        spool = self._spool_now()
        if spool is None:
            return
        try:
            op(spool)
        except Exception:
            logger.exception("spool write failed")

    def _prune(self) -> None:
        cutoff = time.time() - TTL_S
        for job_id in [i for i, j in self._by_id.items() if j.started < cutoff]:
            job = self._by_id.pop(job_id)
            if self._by_key.get(job.key) == job_id:
                del self._by_key[job.key]
            if job.status != "processing":
                self._store(partial(Spool.drop_job, job_id=job_id))
