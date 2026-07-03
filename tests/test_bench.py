"""Seeded spec: model qualification bench (US-104).

Implement ``run_bench`` in lou_op/bench.py: run each task N times from a
clean repo state, aggregate pass rate and iteration counts. Answers "is this
model good enough for this repo" empirically.
"""

from __future__ import annotations

from pathlib import Path

from lou_op.backends.mock import MockBackend
from lou_op.bench import run_bench
from lou_op.models import Task


def test_bench_aggregates_runs(repo: Path) -> None:
    task = Task(
        name="Calculator add()",
        success_criteria=["python -m pytest -q"],
        max_iterations=3,
    )
    report = run_bench(repo, [task], MockBackend(), runs=2)

    assert len(report.task_stats) == 1
    stats = report.task_stats[0]
    assert stats.name == "Calculator add()"
    assert stats.runs == 2
    assert stats.passes == 2  # mock solves it every time
    assert stats.pass_rate == 1.0
    assert stats.mean_iterations >= 1.0


def test_bench_runs_are_isolated(repo: Path) -> None:
    """Each run starts from the same clean state — files from run 1 must not
    leak into run 2 (reset via git between runs)."""
    task = Task(
        name="Calculator add()",
        success_criteria=["python -m pytest -q"],
        max_iterations=3,
    )
    run_bench(repo, [task], MockBackend(), runs=2)
    # after bench, the repo is back to its pre-bench state
    assert not (repo / "calc.py").exists()


def test_preflight_not_counted_as_iteration(repo):
    """A vacuously-green task passes at preflight (iteration 0) — the mean
    must report 0 model iterations, not 1."""
    from lou_op.backends.mock import MockBackend

    task = Task(
        name="vacuous",
        success_criteria=["true"],  # green before any work
        max_iterations=3,
        allow_no_validators=False,
    )
    report = run_bench(repo, [task], MockBackend(), runs=2)
    stats = report.task_stats[0]
    assert stats.passes == 2
    assert stats.mean_iterations == 0


def test_bench_honors_strict_scope(repo):
    """strict_scope in settings must change bench results exactly as it
    changes run results (fail-closed when nothing is inferable)."""
    from lou_op.backends.mock import MockBackend
    from lou_op.config import Settings

    task = Task(
        name="no-scope",
        description="Make everything better.",  # nothing inferable
        success_criteria=["test -f impl.py"],  # red until impl exists
        max_iterations=2,
    )
    settings = Settings()
    settings.strict_scope = True
    report = run_bench(repo, [task], MockBackend(), runs=1, settings=settings)
    assert report.task_stats[0].passes == 0  # failed closed, as a run would
