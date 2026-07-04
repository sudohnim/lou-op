"""Spec (C2): JobSpec.timeout_seconds is a hard wall-clock ceiling.

Previously declared and silently ignored — a runaway job could burn tokens
for days. Enforced at iteration boundaries in run_task and before task
start in the orchestrator.
"""

from __future__ import annotations

import time
from pathlib import Path

from lou_op.backends.base import Backend
from lou_op.loop import run_task
from lou_op.models import IterationContext, IterationOutput, Task, ValidationResult


class _NeverPass:
    name = "never"

    def run(self, repo_path: Path) -> ValidationResult:
        return ValidationResult(name=self.name, passed=False, output="nope")


class _CountingBackend(Backend):
    name = "counting"
    include_code = False
    raw_api = False

    def __init__(self) -> None:
        self.calls = 0

    def run_iteration(self, ctx: IterationContext) -> IterationOutput:
        self.calls += 1
        (ctx.repo_path / f"f{self.calls}.py").write_text("x = 1\n")
        return IterationOutput(done=False, summary=f"Wrote: f{self.calls}.py", log="")


def test_expired_deadline_stops_before_any_iteration(repo: Path) -> None:
    backend = _CountingBackend()
    task = Task(name="t", success_criteria=["true"], max_iterations=5)
    results = run_task(
        repo,
        task,
        backend,
        validators=[_NeverPass()],
        deadline=time.monotonic() - 1,  # already past
    )
    assert backend.calls == 0
    assert not any(r.iteration > 0 and r.done for r in results)


def test_deadline_checked_between_iterations(repo: Path, monkeypatch) -> None:
    """First iteration runs; deadline passes during it; second never starts.
    Deterministic: fake clock advances 10s per check — no wall-time races."""
    import lou_op.loop as loop_mod

    now = [1000.0]

    def fake_monotonic() -> float:
        now[0] += 10.0
        return now[0]

    monkeypatch.setattr(loop_mod.time, "monotonic", fake_monotonic)
    backend = _CountingBackend()
    task = Task(name="t", success_criteria=["true"], max_iterations=5)
    run_task(
        repo,
        task,
        backend,
        validators=[_NeverPass()],
        # first boundary check reads 1010 (ok), second reads 1020 (over)
        deadline=1015.0,
    )
    assert backend.calls == 1  # got one shot, not five


def test_no_deadline_means_no_ceiling(repo: Path) -> None:
    backend = _CountingBackend()
    task = Task(name="t", success_criteria=["true"], max_iterations=3)
    run_task(repo, task, backend, validators=[_NeverPass()])
    assert backend.calls == 3
