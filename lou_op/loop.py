"""The Ralph loop: backend-agnostic body, one commit per iteration."""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import TYPE_CHECKING, Callable, List, Optional

from . import AUTHOR
from .backends.base import Backend
from .exec import retry_with_backoff
from .git_ops import commit_all
from .judge import ConsistencyJudge
from .models import (
    IterationContext,
    IterationResult,
    Task,
    ValidationResult,
)
from .progress import read_progress, write_scratchpad
from .prompts import build_prompt
from .state import render_state
from .validators import Validator, build_validators

if TYPE_CHECKING:
    from .workspace import Workspace


def _status_tag(passed: bool, done: bool) -> str:
    if passed:
        return "✓"
    return "done" if done else "tests failing"


def _has_uncommitted_changes(repo_path: Path) -> bool:
    """True if the backend wrote any files (checked before committing)."""
    result = subprocess.run(
        ["git", "status", "--porcelain"],
        cwd=repo_path,
        capture_output=True,
        text=True,
    )
    return bool(result.stdout.strip())


def run_task(
    repo_path: Path,
    task: Task,
    backend: Backend,
    *,
    workspace: Optional["Workspace"] = None,
    validators: Optional[List[Validator]] = None,
    consistency_judge: Optional[ConsistencyJudge] = None,
    budget: int = 100_000,
    timeout: int = 300,
    on_line: Optional[Callable[[str], None]] = None,
) -> List[IterationResult]:
    """Iterate on ``task`` until it passes, signals done, or hits the cap."""
    emit = on_line or (lambda _: None)
    checks = validators if validators is not None else build_validators(task, timeout)
    results: List[IterationResult] = []
    last_validation: List[ValidationResult] = []
    last_wrote_files: bool = True  # assume first iteration will be productive
    last_claimed_done: bool = False  # whether the model signalled done last iteration
    work_path = workspace.path if workspace is not None else repo_path

    for iteration in range(1, task.max_iterations + 1):
        # B — no-op short circuit
        # If nothing was written AND tests still failing AND model didn't claim done:
        # true no-op — stop immediately, no point continuing
        # If nothing was written AND model claimed done: model is wrong — warn and continue
        if iteration > 1 and not last_wrote_files:
            if not last_claimed_done:
                emit(
                    "[loop] no files written last iteration and tests still failing"
                    " — stopping (manual intervention needed)"
                )
                break
            # else: model claimed done but tests fail — inject a correction below

        # judge: consistency check before each iteration (skip first — no history yet)
        if iteration > 1 and consistency_judge is not None:
            emit("[judge] checking consistency...")
            consistency_judge.check(work_path)  # raises JudgeAbort on mismatch
            emit("[judge] consistent — continuing")

        progress = read_progress(work_path)
        done_correction = (
            "\n\n⚠️  TESTS ARE STILL FAILING. Do NOT output the done signal."
            " You MUST write files to fix every failing test."
            if iteration > 1 and not last_wrote_files and last_claimed_done
            else ""
        )
        state = render_state(
            work_path,
            task,
            progress=progress + done_correction,
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

        # detect whether backend actually wrote any files (before commit)
        last_wrote_files = _has_uncommitted_changes(work_path)
        last_claimed_done = output.done

        last_validation = [check.run(work_path) for check in checks]
        passed = all(v.passed for v in last_validation)
        done = passed  # model's done signal is advisory; validators are the gate

        # Write progress.md — prefer model's scratchpad, fall back to loop-generated entry
        if output.scratchpad:
            write_scratchpad(work_path, output.scratchpad)
        else:
            existing = read_progress(work_path)
            val_summary = "; ".join(
                f"{'PASS' if v.passed else 'FAIL'}: {v.name}" for v in last_validation
            ) if last_validation else "no validators"
            val_output = "\n".join(
                f"  {v.output[:300]}" for v in last_validation if not v.passed
            )
            entry = (
                f"\n## Iteration {iteration} — {_status_tag(passed, done)}\n"
                f"**Files:** {output.summary}\n"
                f"**Validators:** {val_summary}\n"
                + (f"**Errors:**\n{val_output}\n" if val_output else "")
            )
            write_scratchpad(work_path, (existing + entry).strip())

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
