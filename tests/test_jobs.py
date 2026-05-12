"""Tests for jobs.registry and jobs.runner."""

from __future__ import annotations

import threading
import time

import pytest

from card_catalog.jobs.registry import JobRegistry, JobState, JobStatus, registry
from card_catalog.jobs.runner import run_in_thread


def test_create_and_get():
    r = JobRegistry()
    j = r.create("kind1", "label", total=10)
    assert isinstance(j, JobState)
    assert j.status == JobStatus.PENDING
    fetched = r.get(j.id)
    assert fetched is j


def test_update_marks_running_and_sets_done():
    r = JobRegistry()
    j = r.create("k", "lbl", total=5)
    r.update(j.id, done=2, detail="halfway")
    assert j.done == 2
    assert j.status == JobStatus.RUNNING
    assert j.detail == "halfway"


def test_complete_fills_done_and_terminal():
    r = JobRegistry()
    j = r.create("k", "lbl", total=5)
    r.complete(j.id, detail="finished")
    assert j.status == JobStatus.COMPLETED
    assert j.done == 5
    assert j.is_terminal
    assert j.finished_at is not None
    assert j.detail == "finished"


def test_fail_records_error():
    r = JobRegistry()
    j = r.create("k", "lbl")
    r.fail(j.id, "boom")
    assert j.status == JobStatus.FAILED
    assert j.error == "boom"
    assert j.is_terminal


def test_find_active_singleton():
    r = JobRegistry()
    a = r.create("k", "1")
    r.start(a.id)
    # While running, find_active returns the same one.
    active = r.find_active("k")
    assert active is a
    # Once terminal, no active.
    r.complete(a.id)
    assert r.find_active("k") is None


def test_list_filters_by_kind_and_sorts_recent_first():
    r = JobRegistry()
    a = r.create("foo", "a")
    time.sleep(0.001)
    b = r.create("foo", "b")
    c = r.create("bar", "c")
    foo_list = r.list(kind="foo")
    assert [j.id for j in foo_list] == [b.id, a.id]
    assert c.id not in {j.id for j in foo_list}


@pytest.mark.parametrize(
    "total, done, expected",
    [
        (0, 0, 0),  # untouched
        (10, 0, 0),
        (10, 5, 50),
        (10, 10, 100),
        (3, 2, 67),  # rounded
    ],
)
def test_progress_pct(total, done, expected):
    j = JobState(id="x", kind="k", label="l", total=total, done=done)
    assert j.progress_pct == expected


def test_progress_pct_completed_with_zero_total():
    j = JobState(id="x", kind="k", label="l", total=0)
    j.status = JobStatus.COMPLETED
    assert j.progress_pct == 100


# ---- run_in_thread (uses the global registry) -----------------------------


def test_run_in_thread_success():
    done_event = threading.Event()

    def _target(job):
        registry.update(job.id, done=1)
        done_event.set()

    job = run_in_thread("test_kind_ok", "label", _target, total=1, singleton=False)
    assert done_event.wait(timeout=2.0)
    # Wait for the wrapper to call complete().
    for _ in range(50):
        if job.is_terminal:
            break
        time.sleep(0.02)
    assert job.status == JobStatus.COMPLETED


def test_run_in_thread_failure_path():
    def _bad(_job):
        raise RuntimeError("kaboom")

    job = run_in_thread("test_kind_bad", "label", _bad, singleton=False)
    for _ in range(50):
        if job.is_terminal:
            break
        time.sleep(0.02)
    assert job.status == JobStatus.FAILED
    assert job.error == "kaboom"


def test_run_in_thread_singleton_returns_existing():
    blocker = threading.Event()

    def _slow(_job):
        blocker.wait(timeout=2.0)

    first = run_in_thread("test_kind_singleton", "first", _slow, singleton=True)
    try:
        second = run_in_thread(
            "test_kind_singleton", "second", _slow, singleton=True
        )
        assert second.id == first.id
    finally:
        blocker.set()
        for _ in range(50):
            if first.is_terminal:
                break
            time.sleep(0.02)
