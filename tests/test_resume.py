"""Spec (P0.3): a crashed job must resume at its interrupted task.

A task left IN_PROGRESS by a crash is re-selectable (dependency rules still
apply, and it takes priority over pending work). The dead ``next_task``
helper is folded into ``select_next_task`` and removed.
"""

from __future__ import annotations

import pytest

from lou_op.models import Task, TaskStatus
from lou_op.orchestrator import DependencyError, select_next_task


def _task(name: str, status: str = "pending", deps: list[str] | None = None) -> Task:
    return Task(
        name=name,
        status=TaskStatus(status),
        depends_on=deps or [],
        success_criteria=["true"],  # satisfy the require-validators gate
    )


def test_in_progress_task_is_resumed() -> None:
    tasks = [_task("a", "in_progress")]
    assert select_next_task(tasks).name == "a"


def test_in_progress_takes_priority_over_pending() -> None:
    tasks = [_task("fresh"), _task("interrupted", "in_progress")]
    assert select_next_task(tasks).name == "interrupted"


def test_crash_resume_scenario() -> None:
    """passed, in_progress (crashed mid-run), pending-dependent — resume order."""
    tasks = [
        _task("a", "passed"),
        _task("b", "in_progress", deps=["a"]),
        _task("c", deps=["b"]),
    ]
    assert select_next_task(tasks).name == "b"
    tasks[1].status = TaskStatus.PASSED
    assert select_next_task(tasks).name == "c"


def test_in_progress_with_unsatisfied_dep_not_selected() -> None:
    """Weird state (crashed while dep un-passed) — dep rules still apply."""
    tasks = [_task("a"), _task("b", "in_progress", deps=["a"])]
    assert select_next_task(tasks).name == "a"


def test_in_progress_with_failed_dep_raises() -> None:
    tasks = [_task("a", "failed"), _task("b", "in_progress", deps=["a"])]
    with pytest.raises(DependencyError):
        select_next_task(tasks)


def test_all_terminal_returns_none() -> None:
    tasks = [_task("a", "passed"), _task("b", "failed")]
    assert select_next_task(tasks) is None


def test_dead_next_task_removed() -> None:
    import lou_op.orchestrator as orch

    assert not hasattr(orch, "next_task")
