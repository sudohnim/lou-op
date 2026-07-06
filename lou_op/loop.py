"""The Ralph loop: backend-agnostic body, one commit per iteration."""

from __future__ import annotations

import time
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
from .progress import read_progress, trim_progress, write_scratchpad
from .prompts import build_prompt
from .state import render_state
from .validators import Validator, build_validators

if TYPE_CHECKING:
    from .workspace import Workspace

from .adapters.workspace_host import HostWorkspace
from .domain.scope import EmptyScopeError, Scope
from .ports.workspace import Workspace as TreeWorkspace


def _status_tag(passed: bool, done: bool) -> str:
    if passed:
        return "✓"
    return "done" if done else "tests failing"


def _snapshot_protected(repo_path: Path, patterns: List[str]) -> dict[str, str]:
    """Capture contents of files matching ``patterns`` at task start."""
    snapshot: dict[str, str] = {}
    for pattern in patterns:
        for path in sorted(repo_path.glob(pattern)):
            if path.is_file():
                rel = str(path.relative_to(repo_path))
                snapshot[rel] = path.read_text(encoding="utf-8")
    return snapshot


def _restore_protected(
    tree: "TreeWorkspace", snapshot: dict[str, str], emit: Callable[[str], None]
) -> None:
    """Rewrite protected files if the model changed or deleted them."""
    for rel, content in snapshot.items():
        try:
            current: Optional[str] = tree.read(rel)
        except (OSError, ValueError):
            current = None
        if current != content:
            emit(f"[guard] restoring protected file: {rel}")
            tree.write(rel, content)


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
    strict_scope: bool = False,
    deadline: Optional[float] = None,
    tree: Optional[TreeWorkspace] = None,
    on_line: Optional[Callable[[str], None]] = None,
) -> List[IterationResult]:
    """Iterate on ``task`` until it passes, signals done, or hits the cap."""
    emit = on_line or (lambda _: None)
    print(f"[loop] run_task START: {task.name}", flush=True)
    if tree is None:
        tree = HostWorkspace(repo_path)
    checks = validators if validators is not None else build_validators(task, timeout)
    print(f"[loop] validators built ({len(checks)})", flush=True)
    results: List[IterationResult] = []
    last_validation: List[ValidationResult] = []
    last_wrote_files: bool = True  # assume first iteration will be productive
    last_claimed_done: bool = False
    work_path = workspace.path if workspace is not None else repo_path
    protected = _snapshot_protected(work_path, task.protected_files)
    print(f"[loop] protected snapshot done ({len(protected)} files)", flush=True)

    # scope policy is a domain object: strict + nothing inferable fails
    # CLOSED there (EmptyScopeError), never unlimited by accident
    try:
        scope_policy = Scope.from_task(
            task.allowed_paths,
            task.protected_files,
            strict=strict_scope,
            description=task.description,
        )
    except EmptyScopeError:
        emit(
            "[guard] strict scope: no allowed_paths and no files named in"
            f" the description of '{task.name}' — failing closed"
        )
        return [
            IterationResult(
                iteration=0, passed=False, done=False, commit="", validations=[]
            )
        ]

    print(f"[loop] running preflight ({len(checks)} checks)...", flush=True)
    # Anti-gaming pre-flight: a healthy spec is red before any work. If the
    # validators already pass, either the task is done (resume) or the spec is
    # vacuous — either way, don't burn model iterations on it.
    if checks:
        preflight = [check.run(work_path) for check in checks]
        preflight_passed = all(v.passed for v in preflight)
        print(f"[loop] preflight done (all_passed={preflight_passed})", flush=True)
        if preflight_passed:
            emit(
                "[guard] validators pass before any work — task already done"
                " or spec is vacuous; skipping model"
            )
            return [
                IterationResult(
                    iteration=0,
                    passed=True,
                    done=True,
                    commit="",
                    validations=preflight,
                )
            ]

    for iteration in range(1, task.max_iterations + 1):
        if deadline is not None and time.monotonic() >= deadline:
            emit(
                f"[guard] job wall-clock timeout hit before iteration"
                f" {iteration} of '{task.name}' — stopping"
            )
            break

        # B — no-op short circuit
        if iteration > 1 and not last_wrote_files:
            if not last_claimed_done:
                emit(
                    "[loop] no files written last iteration and tests still failing"
                    " — stopping (manual intervention needed)"
                )
                break

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
        print(f"[loop] iter {iteration} — rendering state...", flush=True)
        state = render_state(
            work_path,
            task,
            progress=progress + done_correction,
            last_validation=last_validation or None,
            budget=budget,
            include_code=backend.include_code,
        )
        print(f"[loop] iter {iteration} — building prompt...", flush=True)
        prompt = build_prompt(task, state, raw_api=backend.raw_api)
        ctx = IterationContext(
            repo_path=work_path,
            task=task,
            prompt=prompt,
            iteration=iteration,
            progress=progress,
            on_line=on_line,
        )

        print(f"[loop] iter {iteration} — calling backend.run_iteration...", flush=True)
        output = retry_with_backoff(lambda: backend.run_iteration(ctx))
        print(f"[loop] iter {iteration} — backend returned", flush=True)
        last_claimed_done = output.done

        scope_policy.enforce(tree, emit)
        _restore_protected(tree, protected, emit)

        last_wrote_files = bool(tree.changed_paths())
        print(f"[loop] iter {iteration} — wrote_files={last_wrote_files}", flush=True)

        last_validation = [check.run(work_path) for check in checks]
        passed = all(v.passed for v in last_validation)
        done = passed

        print(f"[loop] iter {iteration} — passed={passed}, done={done}", flush=True)

        if output.scratchpad:
            write_scratchpad(work_path, output.scratchpad)
        else:
            existing = read_progress(work_path)
            val_summary = (
                "; ".join(
                    f"{'PASS' if v.passed else 'FAIL'}: {v.name}"
                    for v in last_validation
                )
                if last_validation
                else "no validators"
            )
            val_output = "\n".join(
                f"  {v.output[:300]}" for v in last_validation if not v.passed
            )
            entry = (
                f"\n## Iteration {iteration} — {_status_tag(passed, done)}\n"
                f"**Files:** {output.summary}\n"
                f"**Validators:** {val_summary}\n"
                + (f"**Errors:**\n{val_output}\n" if val_output else "")
            )
            write_scratchpad(work_path, trim_progress(existing + entry))

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
