from __future__ import annotations

from pathlib import Path

from lou_op.backends.mock import MockBackend
from lou_op.git_ops import log
from lou_op.loop import run_task
from lou_op.models import FileWrite, IterationContext, IterationOutput, Task


def test_mock_loop_completes_and_commits(repo: Path):
    task = Task(
        name="Calculator add()",
        description="implement add",
        success_criteria=["python -m pytest -q"],
        max_iterations=3,
    )
    results = run_task(repo, task, MockBackend(), budget=10_000)

    assert results[-1].done
    assert results[-1].passed
    assert (repo / "calc.py").exists()
    # one commit per iteration; mock finishes in a single iteration.
    assert len(results) == 1
    commits = log(repo, 10)
    assert any("iteration 1" in c for c in commits)


def test_progress_file_written(repo: Path):
    # criterion must be red before work — a trivially-green one (e.g. "true")
    # now trips the vacuous-spec guard and skips the model entirely
    task = Task(name="Calculator add()", success_criteria=["python -m pytest -q"])
    run_task(repo, task, MockBackend(), budget=10_000)
    progress = (repo / ".lou-op" / "progress.md").read_text()
    assert progress.strip()  # scratchpad written (content from backend or fallback)


class _FailingThenDoneBackend(MockBackend):
    """Fails validators on iter 1, then writes the passing project."""

    include_code = True
    raw_api = False

    def __init__(self) -> None:
        super().__init__()
        self.calls = 0

    def run_iteration(self, ctx: IterationContext) -> IterationOutput:
        self.calls += 1
        if self.calls == 1:
            from lou_op.protocol import write_files

            write_files(ctx.repo_path, [FileWrite("calc.py", "broken")])
            return IterationOutput(done=False, summary="broken", log="")
        return super().run_iteration(ctx)


def test_loop_iterates_until_pass(repo: Path):
    task = Task(
        name="Calculator add()",
        success_criteria=["python -m pytest -q"],
        max_iterations=4,
    )
    results = run_task(repo, task, _FailingThenDoneBackend(), budget=10_000)
    assert len(results) == 2
    assert not results[0].passed
    assert results[1].passed
