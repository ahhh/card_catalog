"""In-memory job registry. Single-process, thread-safe, single-user safe."""

from __future__ import annotations

import threading
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum

from card_catalog.utils import utc_now


class JobStatus(str, Enum):
    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"


@dataclass
class JobState:
    id: str
    kind: str
    label: str
    status: JobStatus = JobStatus.PENDING
    total: int = 0
    done: int = 0
    detail: str = ""
    error: str | None = None
    started_at: datetime = field(default_factory=utc_now)
    finished_at: datetime | None = None
    extras: dict = field(default_factory=dict)

    @property
    def progress_pct(self) -> int:
        if self.total <= 0:
            return 0 if self.status != JobStatus.COMPLETED else 100
        return int(round(100 * self.done / self.total))

    @property
    def is_terminal(self) -> bool:
        return self.status in (JobStatus.COMPLETED, JobStatus.FAILED)


class JobRegistry:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._jobs: dict[str, JobState] = {}

    def create(self, kind: str, label: str, total: int = 0) -> JobState:
        job = JobState(id=uuid.uuid4().hex[:12], kind=kind, label=label, total=total)
        with self._lock:
            self._jobs[job.id] = job
        return job

    def get(self, job_id: str) -> JobState | None:
        with self._lock:
            return self._jobs.get(job_id)

    def list(self, kind: str | None = None, limit: int = 20) -> list[JobState]:
        with self._lock:
            jobs = list(self._jobs.values())
        if kind:
            jobs = [j for j in jobs if j.kind == kind]
        jobs.sort(key=lambda j: j.started_at, reverse=True)
        return jobs[:limit]

    def find_active(self, kind: str) -> JobState | None:
        """Return any non-terminal job of the given kind (singleton guard)."""
        with self._lock:
            for j in self._jobs.values():
                if j.kind == kind and not j.is_terminal:
                    return j
        return None

    def update(self, job_id: str, *, done: int | None = None, detail: str | None = None) -> None:
        with self._lock:
            j = self._jobs.get(job_id)
            if not j:
                return
            if done is not None:
                j.done = done
            if detail is not None:
                j.detail = detail
            if j.status == JobStatus.PENDING:
                j.status = JobStatus.RUNNING

    def start(self, job_id: str) -> None:
        with self._lock:
            j = self._jobs.get(job_id)
            if j:
                j.status = JobStatus.RUNNING

    def complete(self, job_id: str, detail: str = "") -> None:
        with self._lock:
            j = self._jobs.get(job_id)
            if j:
                j.status = JobStatus.COMPLETED
                j.done = j.total if j.total else j.done
                j.finished_at = utc_now()
                if detail:
                    j.detail = detail

    def fail(self, job_id: str, error: str) -> None:
        with self._lock:
            j = self._jobs.get(job_id)
            if j:
                j.status = JobStatus.FAILED
                j.error = error
                j.finished_at = utc_now()


registry = JobRegistry()
