"""The Ralph loop: backend-agnostic body, one commit per iteration."""

from __future__ import annotations

import fnmatch
import re
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
from .progress import read_progress, trim_progress, write_scratchpad
from .prompts import build_prompt
from .state import render_state
from .validators import Validator, build_validators

if TYPE_CHECKING:
    from .workspace import Workspace


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
    repo_path: Path, snapshot: dict[str, str], emit: Callable[[str], None]
) -> None:
    """Rewrite protected files if the model changed or deleted them."""
    for rel, content in snapshot.items():
        target = repo_path / rel
        if not target.exists() or target.read_text(encoding="utf-8") != content:
            emit(f"[guard] restoring protected file: {rel}")
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(content, encoding="utf-8")


def _in_scope(rel: str, patterns: List[str]) -> bool:
    for pattern in patterns:
        if fnmatch.fnmatch(rel, pattern):
            return True
        # "dir/**" — fnmatch has no recursive glob, treat as prefix match
        if pattern.endswith("/**") and rel.startswith(pattern[:-3].rstrip("/") + "/"):
            return True
    return False


def _changed_paths(repo_path: Path) -> List[tuple[str, bool]]:
    """``(relative_path, is_untracked)`` for every dirty path in the repo.

    Uses ``-z`` (NUL-delimited): no C-style quoting of filenames with
    spaces/unicode, and rename entries carry the original path as a separate
    NUL-separated field. A rename contributes BOTH sides — the new path (to
    remove if out of scope) and the original (to restore).
    """
    result = subprocess.run(
        ["git", "status", "-z", "--porcelain"],
        cwd=repo_path,
        capture_output=True,
        text=True,
    )
    changed: List[tuple[str, bool]] = []
    fields = result.stdout.split("\0")
    i = 0
    while i < len(fields):
        entry = fields[i]
        i += 1
        if not entry:
            continue
        status, rel = entry[:2], entry[3:]
        changed.append((rel, status == "??"))
        if status[0] in ("R", "C") and i < len(fields):  # rename/copy: orig next
            changed.append((fields[i], False))
            i += 1
    return changed


def _in_head(repo_path: Path, rel: str) -> bool:
    result = subprocess.run(
        ["git", "cat-file", "-e", f"HEAD:{rel}"],
        cwd=repo_path,
        capture_output=True,
    )
    return result.returncode == 0


def _revert_one(repo_path: Path, rel: str, untracked: bool) -> None:
    """Return one path to its HEAD state (or oblivion, if it isn't in HEAD)."""
    target = repo_path / rel
    if untracked:
        if target.is_file():
            target.unlink()
        return
    if _in_head(repo_path, rel):
        # unstage + restore work tree from HEAD (covers staged renames' orig)
        subprocess.run(
            ["git", "checkout", "HEAD", "--", rel],
            cwd=repo_path,
            capture_output=True,
        )
    else:
        # staged but not in HEAD (e.g. the new side of a rename): drop it
        subprocess.run(
            ["git", "rm", "-q", "-f", "--cached", rel],
            cwd=repo_path,
            capture_output=True,
        )
        if target.is_file():
            target.unlink()


def _infer_scope(task: Task) -> List[str]:
    """strict_scope with no allowed_paths: files named in the task description
    are the scope. Anything the model touches beyond them gets reverted."""
    return re.findall(r"[\w./-]*\w\.\w+", task.description)


def _revert_out_of_scope(
    repo_path: Path,
    allowed: List[str],
    emit: Callable[[str], None],
) -> None:
    """Undo model changes outside ``allowed`` globs (``.lou-op/`` is exempt)."""
    if not allowed:
        return
    for rel, untracked in _changed_paths(repo_path):
        if rel.startswith(".lou-op/") or _in_scope(rel, allowed):
            continue
        emit(f"[guard] reverting out-of-scope change: {rel}")
        _revert_one(repo_path, rel, untracked)


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
    strict_scope: bool = False,
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
    protected = _snapshot_protected(work_path, task.protected_files)
    # strict mode: no declared scope ≠ unlimited scope — infer it from the
    # files the task description names
    scope = task.allowed_paths
    if strict_scope and not scope:
        scope = _infer_scope(task)

    # Anti-gaming pre-flight: a healthy spec is red before any work. If the
    # validators already pass, either the task is done (resume) or the spec is
    # vacuous — either way, don't burn model iterations on it.
    if checks:
        preflight = [check.run(work_path) for check in checks]
        if all(v.passed for v in preflight):
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
        # B — no-op short circuit
        # If nothing was written AND tests still failing AND model didn't claim done:
        # true no-op — stop immediately, no point continuing
        # If nothing written AND model claimed done: model is wrong — warn, continue
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
        last_claimed_done = output.done

        # guards run before anything is measured or validated:
        # out-of-scope changes are reverted, tampered spec files restored
        _revert_out_of_scope(work_path, scope, emit)
        _restore_protected(work_path, protected, emit)

        # "wrote files" now means work that *survived* the guards
        last_wrote_files = _has_uncommitted_changes(work_path)

        last_validation = [check.run(work_path) for check in checks]
        passed = all(v.passed for v in last_validation)
        done = passed  # model's done signal is advisory; validators are the gate

        # Write progress.md — prefer model's scratchpad, else loop-generated entry
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
