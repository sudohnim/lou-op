"""P2+P6 spec: the pure domain against in-memory fakes — no git, no model,
no sandbox, no disk."""

from __future__ import annotations

import pytest

from lou_op.domain import Scope, TaskGraph
from lou_op.domain.graph import Node, schedule
from lou_op.domain.scope import EmptyScopeError
from lou_op.ports.workspace import Changed, ExecResult


class FakeTree:
    """In-memory Workspace fake: files dict + scripted exec results."""

    def __init__(self, files=None, exec_pass=True):
        self.files = dict(files or {})
        self.exec_pass = exec_pass
        self.execs: list[str] = []
        self.restored: list[str] = []
        self._changes: list[Changed] = []

    def set_changes(self, changes):
        self._changes = changes

    # port surface used by domain
    def exec(self, command, *, timeout=300, deadline=None):
        self.execs.append(command)
        ok = (
            self.exec_pass
            if isinstance(self.exec_pass, bool)
            else self.exec_pass(command)
        )
        return ExecResult(0 if ok else 1, "out", "")

    def changed_paths(self):
        return list(self._changes)

    def restore_paths(self, paths):
        self.restored.extend(paths)

    def read(self, rel):
        return self.files[rel]

    def write(self, rel, content):
        self.files[rel] = content


class TestTaskGraph:
    def test_diamond_ordering(self):
        g = TaskGraph(
            [Node("a"), Node("b", ("a",)), Node("c", ("a",)), Node("d", ("b", "c"))]
        )
        assert g.ready({}) == ["a"]
        assert g.ready({"a": "passed"}) == ["b", "c"]
        assert g.ready({"a": "passed", "b": "passed", "c": "passed"}) == ["d"]

    def test_resume_first(self):
        g = TaskGraph([Node("a"), Node("b")])
        assert g.ready({"a": "pending", "b": "in_progress"}) == ["b", "a"]

    def test_cycle_rejected(self):
        from lou_op.domain.graph import GraphError

        with pytest.raises(GraphError, match="cycle"):
            TaskGraph([Node("a", ("b",)), Node("b", ("a",))])

    def test_unknown_dep_rejected(self):
        from lou_op.domain.graph import GraphError

        with pytest.raises(GraphError, match="unknown"):
            TaskGraph([Node("a", ("ghost",))])

    def test_blocked_by_failed_dep(self):
        g = TaskGraph([Node("a"), Node("b", ("a",))])
        assert g.blocked({"a": "failed"}) == ["b"]


class TestScheduler:
    def test_pure_schedule_respects_bound_and_failfast(self):
        g = TaskGraph([Node("a"), Node("b"), Node("c")])
        assert schedule(g, {}, [], max_parallel=2) == ["a", "b"]
        assert schedule(g, {}, ["a"], max_parallel=2) == ["b"]
        assert schedule(g, {}, [], max_parallel=2, failed=True) == []

    def test_serial_reproduces_order(self):
        g = TaskGraph([Node("a"), Node("b")])
        assert schedule(g, {}, [], max_parallel=1) == ["a"]


class TestScope:
    def test_fail_closed_when_nothing_inferable(self):
        with pytest.raises(EmptyScopeError):
            Scope.from_task([], [], strict=True, description="Make it better.")

    def test_strict_infers_from_description(self):
        s = Scope.from_task([], [], strict=True, description="Implement impl.py now.")
        assert s.permits("impl.py") and not s.permits("sneaky.py")

    def test_enforce_reverts_rename_both_sides(self):
        tree = FakeTree()
        tree.set_changes([Changed(path="b.txt", status="renamed", old_path="a.txt")])
        s = Scope(allowed=["impl.py"])
        reverted = s.enforce(tree)
        assert set(reverted) == {"a.txt", "b.txt"}
        assert set(tree.restored) == {"a.txt", "b.txt"}

    def test_exempt_and_nonstrict(self):
        s = Scope(allowed=["impl.py"])
        assert s.permits(".lou-op/audit.jsonl")
        assert Scope(allowed=[]).permits("anything.py")
