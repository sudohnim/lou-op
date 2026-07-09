"""The Ralph loop: backend-agnostic body, one commit per iteration."""

from __future__ import annotations

import os
import shutil
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, List, Optional

from . import AUTHOR
from .backends.base import Backend
from .exec import retry_with_backoff, run_command
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
from .ports.workspace import Workspace as TreeWorkspace


def _status_tag(passed: bool, done: bool) -> str:
    if passed:
        return "✓"
    return "done" if done else "tests failing"


log = get_logger()


@dataclass
class _Stop:
    """Sentinel returned by ``_run_iteration`` to end the loop with a reason."""

    reason: str


# Build-output dirs conventionally regenerated from source. Cleared before
# every gate so a stale artifact can never satisfy a test that should rebuild
# it (e.g. `vite preview` serving an old dist/). Ecosystem-agnostic; the same
# invariant holds for compiled binaries, bundlers, and static-site builders.
_BUILD_ARTIFACT_DIRS = ("dist", "build", "target", ".next", "out", ".svelte-kit")


def _clean_build_artifacts(work_path: Path) -> None:
    """Remove known build-output dirs that git does not track, so a gate runs
    against freshly built (or absent) artifacts, never stale ones. Only
    untracked dirs are touched — a project that commits a ``build/`` source
    tree is left alone."""
    for name in _BUILD_ARTIFACT_DIRS:
        target = work_path / name
        if not target.is_dir():
            continue
        tracked = run_command(["git", "ls-files", name], work_path)
        if tracked.stdout.strip():
            continue  # git tracks files here → it's source, not an artifact
        shutil.rmtree(target, ignore_errors=True)


# Dependency dirs that are (correctly) git-ignored but must be present for a
# gate to execute — symlinked into the clean checkout so runners work.
_DEP_DIRS = ("node_modules", ".venv", "venv", "vendor")

# Ignored paths that are legitimately absent from a commit (deps, build output,
# tooling) — not the cause of a broken clean checkout.
_IGNORED_OK = _DEP_DIRS + ("dist", "build", "target", ".next", "out", ".git")


def _ignored_source_files(work_path: Path) -> List[str]:
    """Git-ignored paths that look like SOURCE (not deps/build) — the usual
    reason a clean checkout fails: source the .gitignore silently drops."""
    res = run_command(["git", "status", "--ignored", "--porcelain"], work_path)
    out: List[str] = []
    for line in res.stdout.splitlines():
        if not line.startswith("!!"):
            continue
        path = line[3:].strip().rstrip("/")
        head = path.split("/", 1)[0]
        if head in _IGNORED_OK or path in _IGNORED_OK:
            continue
        out.append(path)
    return out


def _clean_checkout_validate(
    work_path: Path, task: "Task", timeout: int
) -> List[ValidationResult]:
    """Run the gate against ONLY what git would commit.

    Materialises the staged tree (tracked, non-ignored files) into a temp dir
    via ``checkout-index`` and runs the task's validators there, dependency
    dirs symlinked in. Catches a commit that is not self-contained — source
    that .gitignore silently drops, or files left uncommitted — which passes
    against the dirty working tree but fails from a clean checkout.
    """
    run_command(["git", "add", "-A"], work_path)
    tmp = Path(tempfile.mkdtemp(prefix="lou-clean-"))
    try:
        run_command(
            ["git", f"--work-tree={tmp}", "checkout-index", "-a", "-f"], work_path
        )
        for dep in _DEP_DIRS:
            src = work_path / dep
            if src.exists() and not (tmp / dep).exists():
                try:
                    os.symlink(src, tmp / dep)
                except OSError:
                    pass
        # host-cwd validators (no tree shell_fn) execute at the clean tmp dir
        return [check.run(tmp) for check in build_validators(task, timeout)]
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


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
    build_checks: Optional[List[Validator]] = None,
    consistency_judge: Optional[ConsistencyJudge] = None,
    budget: int = 100_000,
    timeout: int = 300,
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
            build_checks=build_checks,
            consistency_judge=consistency_judge,
            budget=budget,
            timeout=timeout,
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
    build_checks: Optional[List[Validator]] = None,
    consistency_judge: Optional[ConsistencyJudge] = None,
    budget: int = 100_000,
    timeout: int = 300,
    deadline: Optional[float] = None,
    tree: Optional[TreeWorkspace] = None,
) -> List[IterationResult]:
    log.info("task started", description=task.description[:80])
    if tree is None:
        tree = HostWorkspace(repo_path)
    checks = validators if validators is not None else build_validators(task, timeout)
    # Structural gate that drives the build phase (compile/typecheck). Empty =>
    # no separate build phase; the acceptance gate drives directly.
    build_checks = build_checks or []
    log.debug("validators built", count=len(checks), build_checks=len(build_checks))
    results: List[IterationResult] = []
    last_validation: List[ValidationResult] = []
    last_wrote_files: bool = True  # assume first iteration will be productive
    last_claimed_done: bool = False
    work_path = workspace.path if workspace is not None else repo_path
    protected = _snapshot_protected(work_path, task.protected_files)
    log.debug("protected snapshot", files=len(protected))

    # Anti-gaming pre-flight: a healthy spec is red before any work. If the
    # validators already pass, either the task is done (resume) or the spec is
    # vacuous — either way, don't burn model iterations on it.
    if checks:
        _clean_build_artifacts(work_path)
        preflight = [check.run(work_path) for check in checks]
        # A gate that can't execute yet (missing runner, wrong command) is NOT
        # fatal: the agent's job — especially the scaffold task — is to CREATE
        # the environment (install deps, write config, use the right binary).
        # Surface it as a hint and let the agent work; no-progress + max-turns
        # bound any spelunking. Aborting here pre-empts a fix the agent could
        # make (e.g. `python` missing but `python3` present) and is more brittle
        # than a model running interactively.
        errored = [v for v in preflight if v.errored]
        if errored:
            log.warning(
                "gate cannot execute yet — letting the agent set up the"
                " environment (install deps / fix the command)",
                phase="guard",
                validators=", ".join(v.name for v in errored),
                output=errored[0].output[-800:],
            )
            # seed last_validation so the agent sees the error and can fix it
            last_validation = preflight
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
                    wrote_files=False,
                    stop_reason="already_passing",
                )
            ]

    stop_reason = "max_iterations"
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
                build_checks,
                protected,
                consistency_judge,
                budget,
                deadline,
                last_validation,
                last_wrote_files,
                last_claimed_done,
                timeout,
            )
        if isinstance(result, _Stop):
            stop_reason = result.reason
            break
        iter_result, last_validation, last_wrote_files, last_claimed_done = result
        results.append(iter_result)
        if iter_result.done:
            stop_reason = "passed" if iter_result.passed else "done"
            break

    # Stamp the terminal reason on the last record so the Store (and `lou-op
    # why`) can report why the task ended, even when it stopped mid-flight.
    if results:
        results[-1].stop_reason = stop_reason
    else:
        results.append(
            IterationResult(
                iteration=0,
                passed=False,
                done=False,
                commit="",
                validations=[],
                wrote_files=False,
                stop_reason=stop_reason,
            )
        )
    return results


def _run_iteration(
    iteration: int,
    task: Task,
    backend: Backend,
    tree: TreeWorkspace,
    work_path: Path,
    workspace: Optional["Workspace"],
    checks: List[Validator],
    build_checks: List[Validator],
    protected: dict[str, str],
    consistency_judge: Optional[ConsistencyJudge],
    budget: int,
    deadline: Optional[float],
    last_validation: List[ValidationResult],
    last_wrote_files: bool,
    last_claimed_done: bool,
    timeout: int,
):
    """One iteration. Returns a ``_Stop`` to end the loop with a reason, else
    the tuple ``(result, last_validation, last_wrote_files, last_claimed_done)``."""
    if deadline is not None and time.monotonic() >= deadline:
        log.warning("job wall-clock timeout — stopping", phase="guard")
        return _Stop("job_timeout")

    # B — no-op short circuit
    if iteration > 1 and not last_wrote_files and not last_claimed_done:
        log.warning(
            "no files written and tests still failing — stopping"
            " (manual intervention needed)"
        )
        return _Stop("no_progress")

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

    # The exam is frozen: restore any protected spec file the model touched.
    # No file-scope fence — the model may write anything; the gate is what
    # judges it, not a write-boundary policy.
    _restore_protected(tree, protected)

    last_wrote_files = bool(tree.changed_paths())

    _clean_build_artifacts(work_path)
    # BUILD-then-LOCK. The structural gate (compile/typecheck) drives the build:
    # while it's red, the agent sees ONLY build errors and builds toward the
    # task intent — the behavior test is not even run, so it can't become the
    # thing the model games/stubs to. Once it builds, the behavior test
    # (`checks`) gates and locks the feature. No build_checks => legacy: the
    # behavior test drives directly.
    structural = [c.run(work_path) for c in build_checks]
    if build_checks and not all(v.passed for v in structural):
        last_validation = structural
        passed = False
    else:
        behavior = [check.run(work_path) for check in checks]
        last_validation = structural + behavior
        passed = all(v.passed for v in behavior)

    # Reproducibility gate: a green working tree isn't enough — the COMMIT must
    # pass too. Re-run the gate against only what git would commit, so source
    # that .gitignore drops or the model left uncommitted fails here instead of
    # shipping a branch that won't build from a clean checkout. Host only (the
    # clean checkout runs on the host, not inside a docker/modal sandbox).
    if passed and checks and isinstance(tree, HostWorkspace):
        clean = _clean_checkout_validate(work_path, task, timeout)
        if not all(v.passed for v in clean):
            passed = False
            # Point the model at the actual cause: files present in its working
            # tree but git-ignored, so absent from the commit. Prepended so it
            # survives output truncation and lands in "Last Validation Output".
            ignored = _ignored_source_files(work_path)
            note = ""
            if ignored:
                note = (
                    "CLEAN-CHECKOUT FAILED: these paths exist in your working"
                    " tree but are GIT-IGNORED, so the commit omits them and it"
                    " won't build from a clean clone: "
                    + ", ".join(ignored[:10])
                    + ". Fix .gitignore (remove the offending rule) or move the"
                    " files out of the ignored path, then rewrite them.\n\n"
                )
            for v in clean:
                if not v.passed:
                    v.output = note + v.output
            last_validation = clean
            log.error(
                "passed on the working tree but FAILED a clean checkout — the"
                " commit is not self-contained",
                phase="validate",
                ignored_paths=ignored,
                output=clean[0].output[:800] if clean else "",
            )

    done = passed
    log.info(
        "iteration complete",
        phase="validate",
        passed=passed,
        wrote_files=last_wrote_files,
    )
    # Surface WHY the gate is red to the operator, not just passed=False. The
    # ERROR/FAIL split tells "broken runner" from "wrong code" at a glance.
    if not passed:
        for v in last_validation:
            if v.errored:
                log.error(
                    "gate ERROR — runner could not execute",
                    phase="validate",
                    validator=v.name,
                    output=v.output[-800:],
                )
            elif not v.passed:
                log.warning(
                    "gate FAILED",
                    phase="validate",
                    validator=v.name,
                    output=v.output[-800:],
                )

    if output.scratchpad:
        write_scratchpad(work_path, output.scratchpad)
    else:
        # Accumulated history: what the model did each iteration + a one-line
        # PASS/FAIL trend. The full failure output is NOT echoed here — it is
        # already fed to the model verbatim by render_state's "Last Validation
        # Output" section, so duplicating it into progress.md was dead weight.
        existing = read_progress(work_path)
        val_summary = (
            "; ".join(
                f"{'PASS' if v.passed else 'FAIL'}: {v.name}" for v in last_validation
            )
            if last_validation
            else "no validators"
        )
        entry = (
            f"\n## Iteration {iteration} — {_status_tag(passed, done)}\n"
            f"**Files:** {output.summary}\n"
            f"**Validators:** {val_summary}\n"
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
        wrote_files=last_wrote_files,
    )
    return iter_result, last_validation, last_wrote_files, last_claimed_done
