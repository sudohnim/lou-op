"""The Ralph loop: backend-agnostic body, one commit per iteration."""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Callable, List, Optional

from . import AUTHOR
from .backends.base import Backend
from .exec import retry_with_backoff
from .git_ops import commit_all
from .models import (
    IterationContext,
    IterationResult,
    Task,
    ValidationResult,
)
from .progress import append_progress, read_progress
from .prompts import build_prompt
from .state import render_state
from .validators import Validator, build_validators

if TYPE_CHECKING:
    from .workspace import Workspace


def _status_tag(passed: bool, done: bool) -> str:
    if passed:
        return "✓"
    return "done" if done else "tests failing"


def run_task(
    repo_path: Path,
    task: Task,
    backend: Backend,
    *,
    workspace: Optional["Workspace"] = None,
    validators: Optional[List[Validator]] = None,
    budget: int = 100_000,
    timeout: int = 300,
    on_line: Optional[Callable[[str], None]] = None,
) -> List[IterationResult]:
    """Iterate on ``task`` until it passes, signals done, or hits the cap."""
    checks = validators if validators is not None else build_validators(task, timeout)
    results: List[IterationResult] = []
    last_validation: List[ValidationResult] = []
    work_path = workspace.path if workspace is not None else repo_path

    for iteration in range(1, task.max_iterations + 1):
        progress = read_progress(work_path)
        state = render_state(
            work_path,
            task,
            progress=progress,
            last_validation=last_validation or None,
            budget=budget,
            include_code=backend.include_code,
        )
        prompt = build_prompt(task, state, raw_api=backend.raw_api)
        ctx = IterationContext(
            repo_path=work_path,
            task=task,
            prompt=prompt,
            iteration=iteration,
            progress=progress,
            on_line=on_line,
        )

        output = retry_with_backoff(lambda: backend.run_iteration(ctx))

        last_validation = [check.run(work_path) for check in checks]
        passed = all(v.passed for v in last_validation)
        done = passed or output.done

        append_progress(work_path, task.name, output.summary, iteration)
        commit_msg = f"{task.name}: iteration {iteration} [{_status_tag(passed, done)}]"
        if workspace is not None:
            sha = workspace.checkpoint(commit_msg)
        else:
            sha = commit_all(work_path, commit_msg, AUTHOR)

        results.append(
            IterationResult(
                iteration=iteration,
                passed=passed,
                done=done,
                commit=sha,
                validations=last_validation,
            )
        )
        if done:
            break

    return results
