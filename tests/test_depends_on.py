"""Seeded spec: dependency-aware task selection (US-102).

Implement ``select_next_task`` in lou_op/orchestrator.py and use it in
``_execute`` instead of first-unfinished order.
"""

from __future__ import annotations

import pytest

from lou_op.models import Task, TaskStatus
from lou_op.orchestrator import DependencyError, select_next_task


def _task(name: str, status: str = "pending", deps: list[str] | None = None) -> Task:
    return Task(name=name, status=TaskStatus(status), depends_on=deps or [])


def test_picks_first_pending_without_deps() -> None:
    tasks = [_task("a", "passed"), _task("b"), _task("c")]
    assert select_next_task(tasks).name == "b"


def test_blocked_task_skipped_until_dep_passes() -> None:
    tasks = [_task("b", deps=["a"]), _task("a")]  # listed out of order
    assert select_next_task(tasks).name == "a"
    tasks[1].status = TaskStatus.PASSED
    assert select_next_task(tasks).name == "b"


def test_failed_dependency_blocks_dependent() -> None:
    tasks = [_task("a", "failed"), _task("b", deps=["a"])]
    with pytest.raises(DependencyError):
        select_next_task(tasks)


def test_cycle_raises() -> None:
    tasks = [_task("a", deps=["b"]), _task("b", deps=["a"])]
    with pytest.raises(DependencyError):
        select_next_task(tasks)


def test_unknown_dependency_raises() -> None:
    tasks = [_task("a", deps=["ghost"])]
    with pytest.raises(DependencyError):
        select_next_task(tasks)


def test_all_done_returns_none() -> None:
    tasks = [_task("a", "passed"), _task("b", "passed")]
    assert select_next_task(tasks) is None
