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
Jobs live in memory for a day; a restart mid-note loses that note, and the app
says so rather than pretending it worked.
"""

from __future__ import annotations

import hashlib
import threading
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

TTL_S = 24 * 3600

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


class Jobs:
    def __init__(self, run: Callable[[Callable[[], None]], None]) -> None:
        self._run = run
        self._lock = threading.Lock()
        self._by_id: dict[str, Job] = {}
        self._by_key: dict[str, str] = {}

    def submit(self, key: str, kind: str, work: Callable[[], Reply]) -> tuple[Job, bool]:
        """(job, new): start `work` unless this note already has a live or finished job."""
        with self._lock:
            self._prune()
            existing = self._by_id.get(self._by_key.get(key, ""))
            if existing is not None and existing.status != "failed":
                return existing, False
            job = Job(id=uuid.uuid4().hex[:16], key=key, kind=kind)
            self._by_id[job.id] = job
            self._by_key[key] = job.id

        def go() -> None:
            try:
                reply = work()
            except Exception as exc:  # the handler already logged it
                reply = (500, {"error": "failed", "message": str(exc) or "Logging failed."})
            with self._lock:
                job.reply = reply
                job.status = "done" if reply[0] < 400 else "failed"
                job.finished = time.time()

        self._run(go)
        return job, True

    def get(self, job_id: str) -> Job | None:
        with self._lock:
            return self._by_id.get(job_id)

    def recent(self) -> list[Job]:
        with self._lock:
            self._prune()
            return sorted(self._by_id.values(), key=lambda j: j.started, reverse=True)

    def _prune(self) -> None:
        cutoff = time.time() - TTL_S
        for job_id in [i for i, j in self._by_id.items() if j.started < cutoff]:
            job = self._by_id.pop(job_id)
            if self._by_key.get(job.key) == job_id:
                del self._by_key[job.key]
