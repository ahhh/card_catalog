"""Launch background work on a thread with exception capture."""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable
from typing import Any

from card_catalog.jobs.registry import JobState, registry

log = logging.getLogger(__name__)


def run_in_thread(
    kind: str,
    label: str,
    target: Callable[[JobState], Any],
    *,
    total: int = 0,
    singleton: bool = True,
) -> JobState:
    """Create a job, run `target(job)` in a thread, return the JobState."""
    if singleton:
        existing = registry.find_active(kind)
        if existing:
            return existing
    job = registry.create(kind=kind, label=label, total=total)

    def _wrapper() -> None:
        try:
            registry.start(job.id)
            target(job)
            if not job.is_terminal:
                registry.complete(job.id)
        except Exception as exc:  # noqa: BLE001
            log.exception("job %s (%s) failed", job.id, kind)
            registry.fail(job.id, str(exc))

    threading.Thread(target=_wrapper, daemon=True, name=f"job-{job.id}").start()
    return job
