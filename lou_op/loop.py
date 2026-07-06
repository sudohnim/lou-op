"""The Ralph loop: backend-agnostic body, one commit per iteration."""

from __future__ import annotations

import time
from pathlib import Path
from typing import TYPE_CHECKING, List, Optional

from . import AUTHOR
from .backends.base import Backend
from .exec import retry_with_backoff
from .git_ops import commit_all
from .judge import ConsistencyJudge
from .logutil import bound, get_logger
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


log = get_logger()


def _snapshot_protected(repo_path: Path, patterns: List[str]) -> dict[str, str]:
    """Capture contents of files matching ``patterns`` at task start."""
    snapshot: dict[str, str] = {}
    for pattern in patterns:
        for path in sorted(repo_path.glob(pattern)):
            if path.is_file():
                rel = str(path.relative_to(repo_path))
                snapshot[rel] = path.read_text(encoding="utf-8")
    return snapshot


def _restore_protected(tree: "TreeWorkspace", snapshot: dict[str, str]) -> None:
    """Rewrite protected files if the model changed or deleted them."""
    for rel, content in snapshot.items():
        try:
            current: Optional[str] = tree.read(rel)
        except (OSError, ValueError):
            current = None
        if current != content:
            log.info("restoring protected file", phase="guard", path=rel)
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
) -> List[IterationResult]:
    """Iterate on ``task`` until it passes, signals done, or hits the cap.

    Binds ``task`` into the logging context for the whole call, so every log
    line — here and in the backend — is tagged without threading a callback.
    """
    with bound(task=task.name):
        return _run_task(
            repo_path,
            task,
            backend,
            workspace=workspace,
            validators=validators,
            consistency_judge=consistency_judge,
            budget=budget,
            timeout=timeout,
            strict_scope=strict_scope,
            deadline=deadline,
            tree=tree,
        )


def _run_task(
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
) -> List[IterationResult]:
    log.info("task started", description=task.description[:80])
    if tree is None:
        tree = HostWorkspace(repo_path)
    checks = validators if validators is not None else build_validators(task, timeout)
    log.debug("validators built", count=len(checks))
    results: List[IterationResult] = []
    last_validation: List[ValidationResult] = []
    last_wrote_files: bool = True  # assume first iteration will be productive
    last_claimed_done: bool = False
    work_path = workspace.path if workspace is not None else repo_path
    protected = _snapshot_protected(work_path, task.protected_files)
    log.debug("protected snapshot", files=len(protected))

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
        log.warning(
            "strict scope failing closed: no allowed_paths and no files"
            " named in the description",
            phase="guard",
        )
        return [
            IterationResult(
                iteration=0, passed=False, done=False, commit="", validations=[]
            )
        ]

    # Anti-gaming pre-flight: a healthy spec is red before any work. If the
    # validators already pass, either the task is done (resume) or the spec is
    # vacuous — either way, don't burn model iterations on it.
    if checks:
        preflight = [check.run(work_path) for check in checks]
        preflight_passed = all(v.passed for v in preflight)
        log.debug("preflight done", all_passed=preflight_passed)
        if preflight_passed:
            log.info(
                "validators pass before any work — already done or vacuous"
                " spec; skipping model",
                phase="guard",
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
        with bound(iteration=iteration):
            result = _run_iteration(
                iteration,
                task,
                backend,
                tree,
                work_path,
                workspace,
                checks,
                scope_policy,
                protected,
                consistency_judge,
                budget,
                deadline,
                last_validation,
                last_wrote_files,
                last_claimed_done,
            )
        if result is None:
            break
        iter_result, last_validation, last_wrote_files, last_claimed_done = result
        results.append(iter_result)
        if iter_result.done:
            break

    return results


def _run_iteration(
    iteration: int,
    task: Task,
    backend: Backend,
    tree: TreeWorkspace,
    work_path: Path,
    workspace: Optional["Workspace"],
    checks: List[Validator],
    scope_policy: Scope,
    protected: dict[str, str],
    consistency_judge: Optional[ConsistencyJudge],
    budget: int,
    deadline: Optional[float],
    last_validation: List[ValidationResult],
    last_wrote_files: bool,
    last_claimed_done: bool,
):
    """One iteration. Returns ``None`` to stop the loop, else the tuple
    ``(result, last_validation, last_wrote_files, last_claimed_done)``."""
    if deadline is not None and time.monotonic() >= deadline:
        log.warning("job wall-clock timeout — stopping", phase="guard")
        return None

    # B — no-op short circuit
    if iteration > 1 and not last_wrote_files and not last_claimed_done:
        log.warning(
            "no files written and tests still failing — stopping"
            " (manual intervention needed)"
        )
        return None

    # judge: consistency check before each iteration (skip first — no history)
    if iteration > 1 and consistency_judge is not None:
        log.info("checking consistency", phase="judge")
        consistency_judge.check(work_path)  # raises JudgeAbort on mismatch
        log.info("consistent — continuing", phase="judge")

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
    )

    log.info("generating", phase="generate")
    output = retry_with_backoff(lambda: backend.run_iteration(ctx))
    last_claimed_done = output.done

    reverted = scope_policy.enforce(tree)
    if reverted:
        log.info("reverted out-of-scope changes", phase="guard", paths=reverted)
    _restore_protected(tree, protected)

    last_wrote_files = bool(tree.changed_paths())

    last_validation = [check.run(work_path) for check in checks]
    passed = all(v.passed for v in last_validation)
    done = passed
    log.info(
        "iteration complete",
        phase="validate",
        passed=passed,
        wrote_files=last_wrote_files,
    )

    if output.scratchpad:
        write_scratchpad(work_path, output.scratchpad)
    else:
        existing = read_progress(work_path)
        val_summary = (
            "; ".join(
                f"{'PASS' if v.passed else 'FAIL'}: {v.name}" for v in last_validation
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

    iter_result = IterationResult(
        iteration=iteration,
        passed=passed,
        done=done,
        commit=sha,
        validations=last_validation,
    )
    return iter_result, last_validation, last_wrote_files, last_claimed_done
