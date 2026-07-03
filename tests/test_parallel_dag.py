"""Spec (P1.4): dependency-aware parallel scheduling.

``run_parallel(tasks, run_one, max_parallel=N)`` runs tasks whose deps are
all PASSED, up to N at a time. N=1 must be byte-for-byte the serial order.
Dependents start only after every dependency passes; a failed dependency
permanently blocks its dependents (DependencyError).
"""

from __future__ import annotations

import threading
import time

import pytest

from lou_op.models import Task, TaskStatus
from lou_op.orchestrator import DependencyError, ready_tasks, run_parallel


def _task(name: str, deps: list[str] | None = None) -> Task:
    return Task(name=name, depends_on=deps or [], success_criteria=["true"])


class TestReadyTasks:
    def test_all_independent_are_ready(self) -> None:
        tasks = [_task("a"), _task("b"), _task("c")]
        assert [t.name for t in ready_tasks(tasks)] == ["a", "b", "c"]

    def test_blocked_not_ready(self) -> None:
        tasks = [_task("a"), _task("b", deps=["a"])]
        assert [t.name for t in ready_tasks(tasks)] == ["a"]

    def test_dep_passed_unblocks(self) -> None:
        tasks = [_task("a"), _task("b", deps=["a"])]
        tasks[0].status = TaskStatus.PASSED
        assert [t.name for t in ready_tasks(tasks)] == ["b"]


class TestSerialCompat:
    def test_max_parallel_1_is_serial_in_order(self) -> None:
        order: list[str] = []

        def run_one(task: Task) -> bool:
            order.append(task.name)
            return True

        tasks = [_task("a"), _task("b"), _task("c", deps=["a"])]
        run_parallel(tasks, run_one, max_parallel=1)
        assert order == ["a", "b", "c"]
        assert all(t.status == TaskStatus.PASSED for t in tasks)


class TestParallel:
    def test_concurrency_bounded(self) -> None:
        peak = 0
        active = 0
        lock = threading.Lock()

        def run_one(task: Task) -> bool:
            nonlocal peak, active
            with lock:
                active += 1
                peak = max(peak, active)
            time.sleep(0.05)
            with lock:
                active -= 1
            return True

        tasks = [_task(f"t{i}") for i in range(6)]
        run_parallel(tasks, run_one, max_parallel=2)
        assert peak == 2  # bound hit and never exceeded
        assert all(t.status == TaskStatus.PASSED for t in tasks)

    def test_independent_tasks_do_overlap(self) -> None:
        peak = 0
        active = 0
        lock = threading.Lock()

        def run_one(task: Task) -> bool:
            nonlocal peak, active
            with lock:
                active += 1
                peak = max(peak, active)
            time.sleep(0.05)
            with lock:
                active -= 1
            return True

        run_parallel([_task("a"), _task("b")], run_one, max_parallel=2)
        assert peak == 2  # actually ran concurrently

    def test_dependent_starts_after_dep_finishes(self) -> None:
        events: list[str] = []
        lock = threading.Lock()

        def run_one(task: Task) -> bool:
            with lock:
                events.append(f"start:{task.name}")
            time.sleep(0.05)
            with lock:
                events.append(f"end:{task.name}")
            return True

        tasks = [_task("dep"), _task("late", deps=["dep"]), _task("free")]
        run_parallel(tasks, run_one, max_parallel=3)
        assert events.index("end:dep") < events.index("start:late")

    def test_failed_dep_blocks_dependent(self) -> None:
        ran: list[str] = []

        def run_one(task: Task) -> bool:
            ran.append(task.name)
            return task.name != "bad"

        tasks = [_task("bad"), _task("child", deps=["bad"])]
        with pytest.raises(DependencyError):
            run_parallel(tasks, run_one, max_parallel=2)
        assert "child" not in ran
        assert tasks[0].status == TaskStatus.FAILED
        assert tasks[1].status == TaskStatus.PENDING  # never started

    def test_diamond_ordering(self) -> None:
        """a → (b, c) → d: d runs last, b/c may overlap."""
        events: list[str] = []
        lock = threading.Lock()

        def run_one(task: Task) -> bool:
            with lock:
                events.append(f"start:{task.name}")
            time.sleep(0.02)
            with lock:
                events.append(f"end:{task.name}")
            return True

        tasks = [
            _task("a"),
            _task("b", deps=["a"]),
            _task("c", deps=["a"]),
            _task("d", deps=["b", "c"]),
        ]
        run_parallel(tasks, run_one, max_parallel=4)
        assert events.index("end:a") < events.index("start:b")
        assert events.index("end:a") < events.index("start:c")
        assert events.index("end:b") < events.index("start:d")
        assert events.index("end:c") < events.index("start:d")
