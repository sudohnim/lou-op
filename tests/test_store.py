"""P3 spec: event-sourced Store — durable, versioned, snapshot-bounded."""

from __future__ import annotations

from pathlib import Path

import pytest

from lou_op.adapters.store_sqlite import SqliteStore
from lou_op.domain.graph import Node, TaskGraph
from lou_op.ports.store import Event, RunState, fold


@pytest.fixture()
def store(tmp_path: Path) -> SqliteStore:
    return SqliteStore(tmp_path / "events.db")


def _run_events(store, run_id="r1"):
    store.append(run_id, "run_created", {"tasks": ["a", "b"]})
    store.append(run_id, "run_started", {})
    store.append(run_id, "task_status", {"task": "a", "status": "in_progress"})
    store.append(
        run_id,
        "iteration",
        {
            "task": "a",
            "n": 1,
            "passed": True,
            "commit": "abc",
            "tokens": 500,
            "cost_usd": 0.01,
        },
    )
    store.append(run_id, "task_status", {"task": "a", "status": "passed"})


class TestFold:
    def test_load_reconstructs_state(self, store) -> None:
        _run_events(store)
        state, replayed = store.load("r1")
        assert state.task_status == {"a": "passed", "b": "pending"}
        assert state.status == "running"
        assert state.commits == ["abc"]
        assert state.tokens_total == 500 and state.cost_usd == 0.01
        assert replayed == 5

    def test_unknown_event_kind_skipped(self, store) -> None:
        _run_events(store)
        store.append("r1", "from_the_future", {"payload": 42}, version=9)
        state, _ = store.load("r1")
        assert state.task_status["a"] == "passed"  # fold survived

    def test_old_version_event_still_folds(self) -> None:
        """A v1 'iteration' without tokens/cost fields (older schema) must
        apply cleanly after the shape gained fields."""
        state = RunState()
        old = Event(1, "r", "iteration", 1, {"task": "a", "commit": "sha"}, 0.0)
        fold(state, old)
        assert state.commits == ["sha"]
        assert state.tokens_total == 0


class TestSnapshotBound:
    def test_resume_cost_bounded_by_snapshot(self, store) -> None:
        _run_events(store)
        state, _ = store.load("r1")
        store.save_snapshot("r1", state)
        store.append("r1", "task_status", {"task": "b", "status": "in_progress"})
        state2, replayed = store.load("r1")
        assert replayed == 1  # only the tail after the snapshot
        assert state2.task_status["b"] == "in_progress"
        assert state2.task_status["a"] == "passed"  # carried by snapshot


class TestCrashResume:
    def test_new_process_reconstructs_and_resumes(self, tmp_path: Path) -> None:
        """Kill mid-run: a NEW store instance on the same file rebuilds the
        exact state, and the pure graph resumes the right task with no
        duplicated work."""
        db = tmp_path / "events.db"
        first = SqliteStore(db)
        _run_events(first)  # a passed; b never started; process "dies" here

        second = SqliteStore(db)  # the restarted process
        state, _ = second.load("r1")
        graph = TaskGraph([Node("a"), Node("b", ("a",))])
        assert graph.ready(state.task_status) == ["b"]  # not a again

    def test_run_ids_listing(self, store) -> None:
        _run_events(store, "r1")
        _run_events(store, "r2")
        assert store.run_ids() == ["r1", "r2"]
