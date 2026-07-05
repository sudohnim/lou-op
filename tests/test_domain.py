"""P2+P6 spec: the pure domain against in-memory fakes — no git, no model,
no sandbox, no disk."""

from __future__ import annotations

import pytest

from lou_op.domain import (
    Criterion,
    IterationMachine,
    IterationState,
    Provenance,
    Scope,
    TaskGraph,
    VacuousSpecError,
    Verification,
)
from lou_op.domain.graph import Node, schedule
from lou_op.domain.iteration import AgentReport, GuardReport, VerdictInput
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


class TestIterationMachine:
    def _drive_to_commit(self, m, wrote=True, claimed=False, passed=False):
        m.generated(AgentReport(claimed_done=claimed, wrote_files=wrote))
        m.guarded(GuardReport())
        m.validated(VerdictInput(passed=passed))
        return m.committed()

    def test_happy_path_done(self):
        m = IterationMachine()
        assert self._drive_to_commit(m, passed=True) == IterationState.DONE

    def test_failing_validators_continue(self):
        m = IterationMachine()
        assert self._drive_to_commit(m, passed=False) == IterationState.CONTINUE

    def test_noop_stops(self):
        m = IterationMachine()
        state = self._drive_to_commit(m, wrote=False, claimed=False, passed=False)
        assert state == IterationState.STOP

    def test_wrong_done_claim_continues(self):
        """Claimed done but red: model is wrong — keep iterating (loop
        injects a correction), don't silently stop."""
        m = IterationMachine()
        state = self._drive_to_commit(m, wrote=False, claimed=True, passed=False)
        assert state == IterationState.CONTINUE

    def test_illegal_transition_raises(self):
        m = IterationMachine()
        with pytest.raises(ValueError, match="illegal transition"):
            m.validated(VerdictInput(passed=True))  # skipped GENERATE/GUARD

    def test_interrupt_from_any_state(self):
        for advance in range(3):
            m = IterationMachine()
            if advance > 0:
                m.generated(AgentReport(claimed_done=False, wrote_files=True))
            if advance > 1:
                m.guarded(GuardReport())
            assert m.interrupt() == IterationState.INTERRUPTED
            assert m.is_terminal

    def test_interrupt_does_not_overwrite_terminal(self):
        m = IterationMachine()
        self._drive_to_commit(m, passed=True)
        assert m.interrupt() == IterationState.DONE


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


class TestVerification:
    def test_implementer_cannot_be_authoritative(self):
        with pytest.raises(ValueError, match="cannot be authoritative"):
            Verification(
                [Criterion("test", "pytest -q")],
                provenance=Provenance.IMPLEMENTER,
            )

    def test_implementer_ok_when_advisory(self):
        v = Verification(
            [Criterion("custom", "true")],
            provenance=Provenance.IMPLEMENTER,
            authoritative=False,
        )
        assert not v.authoritative

    def test_authoritative_needs_criteria(self):
        with pytest.raises(ValueError, match="at least one criterion"):
            Verification([], provenance=Provenance.HUMAN)

    def test_evaluate_all_must_pass(self):
        v = Verification(
            [Criterion("test", "pytest a"), Criterion("lint", "flake8")],
            provenance=Provenance.HUMAN,
        )
        verdict = v.evaluate(FakeTree(exec_pass=lambda c: c != "flake8"))
        assert not verdict.passed
        assert [r.passed for r in verdict.results] == [True, False]

    def test_vacuous_spec_rejected(self):
        v = Verification([Criterion("test", "pytest -q")], provenance=Provenance.HUMAN)
        with pytest.raises(VacuousSpecError):
            v.assert_can_fail(FakeTree(exec_pass=True))  # green pre-impl

    def test_red_spec_accepted(self):
        v = Verification([Criterion("test", "pytest -q")], provenance=Provenance.HUMAN)
        v.assert_can_fail(FakeTree(exec_pass=False))  # red = can fail = valid

    def test_judge_signal_is_not_a_verdict_input(self):
        """Type-level: evaluate() takes only the tree — JudgeSignal cannot
        flip a Verdict because there is no parameter to pass it through."""
        import inspect

        from lou_op.domain.verification import Verification as V

        params = inspect.signature(V.evaluate).parameters
        assert "judge" not in params and "signal" not in params
