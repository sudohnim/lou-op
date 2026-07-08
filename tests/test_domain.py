"""P2+P6 spec: the pure domain against in-memory fakes — no git, no model,
no sandbox, no disk."""

from __future__ import annotations

import pytest

from lou_op.domain import TaskGraph
from lou_op.domain.graph import Node, schedule


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
