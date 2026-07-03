"""Benchmarking: run tasks multiple times to measure pass rate and iteration count."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import List

from .backends.base import Backend
from .exec import run_command
from .git_ops import current_commit, revert_to
from .loop import run_task
from .models import Task


@dataclass
class TaskStats:
    name: str
    runs: int
    passes: int
    pass_rate: float
    mean_iterations: float


@dataclass
class BenchReport:
    task_stats: List[TaskStats]


def run_bench(
    repo_path: Path,
    tasks: List[Task],
    backend: Backend,
    *,
    runs: int = 3,
) -> BenchReport:
    """Run each task multiple times to measure pass rate and iteration count.

    Each run starts from a clean state (git reset between runs).
    """
    repo_path = Path(repo_path)

    # Create an empty initial commit if none exists
    try:
        initial_sha = current_commit(repo_path)
    except RuntimeError:
        # No commits yet, create an empty one
        run_command(
            [
                "git",
                "-c",
                "user.name=bench",
                "-c",
                "user.email=bench@lou-op.dev",
                "commit",
                "--allow-empty",
                "-m",
                "bench: initial state",
            ],
            repo_path,
        )
        initial_sha = current_commit(repo_path)

    task_stats = []

    try:
        for task in tasks:
            run_results = []
            total_iterations = 0

            for _ in range(runs):
                # Reset to clean state before each run
                revert_to(repo_path, initial_sha)

                # Run the task
                results = run_task(repo_path, task, backend)

                # Check if the run passed (last result)
                passed = results[-1].passed if results else False
                run_results.append(passed)

                # Count iterations
                total_iterations += len(results)

            # Calculate statistics
            passes = sum(run_results)
            pass_rate = passes / runs if runs > 0 else 0.0
            mean_iterations = total_iterations / runs if runs > 0 else 0.0

            stats = TaskStats(
                name=task.name,
                runs=runs,
                passes=passes,
                pass_rate=pass_rate,
                mean_iterations=mean_iterations,
            )
            task_stats.append(stats)
    finally:
        # Restore repo to initial state
        revert_to(repo_path, initial_sha)

    return BenchReport(task_stats=task_stats)
