"""Serialize repo state into a prompt, under a token budget.

The PRD v0.1 wanted to dump the whole codebase every iteration, which is
incompatible with a 100k-token prompt on any real project. Instead we select
files via git (respecting ``.gitignore``) up to a token budget, prioritizing
files named in the task. The agent-CLI backend reads the repo itself, so it
gets a lighter prompt with ``include_code=False``.
"""

from __future__ import annotations

from pathlib import Path
from typing import List, Optional

from .exec import run_command
from .git_ops import log
from .models import Task, ValidationResult

# Rough heuristic: ~4 characters per token.
_CHARS_PER_TOKEN = 4
_SKIP_SUFFIXES = {
    ".png",
    ".jpg",
    ".jpeg",
    ".gif",
    ".ico",
    ".pdf",
    ".zip",
    ".lock",
    ".so",
    ".pyc",
}


def estimate_tokens(text: str) -> int:
    return len(text) // _CHARS_PER_TOKEN


def list_repo_files(repo_path: Path) -> List[str]:
    """git-tracked + untracked-not-ignored files (respects .gitignore)."""
    result = run_command(
        ["git", "ls-files", "--cached", "--others", "--exclude-standard"],
        repo_path,
    )
    if not result.passed:
        return []
    files = [line for line in result.stdout.splitlines() if line.strip()]
    return sorted(set(files))


def _mentioned_first(files: List[str], task: Task) -> List[str]:
    haystack = f"{task.description} {' '.join(task.success_criteria)}"
    mentioned = [f for f in files if f in haystack]
    rest = [f for f in files if f not in mentioned]
    return mentioned + rest


def _select_files(repo_path: Path, task: Task, budget: int) -> List[str]:
    candidates = _mentioned_first(list_repo_files(repo_path), task)
    selected: List[str] = []
    used = 0
    for rel in candidates:
        path = repo_path / rel
        if path.suffix.lower() in _SKIP_SUFFIXES:
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        cost = estimate_tokens(text)
        if used + cost > budget:
            continue
        used += cost
        selected.append(rel)
    return selected


def render_state(
    repo_path: Path,
    task: Task,
    *,
    progress: str = "",
    last_validation: Optional[List[ValidationResult]] = None,
    budget: int = 100_000,
    include_code: bool = True,
) -> str:
    """Build the state section fed to a backend."""
    parts: List[str] = []

    parts.append("## Current Task")
    parts.append(f"Name: {task.name}")
    if task.description:
        parts.append(f"Description: {task.description}")
    if task.success_criteria:
        crit = "\n".join(f"  - {c}" for c in task.success_criteria)
        parts.append(f"Success criteria (shell commands):\n{crit}")

    if include_code:
        parts.append("\n## Current Codebase")
        files = _select_files(repo_path, task, budget)
        if not files:
            parts.append("(empty repository)")
        for rel in files:
            content = (repo_path / rel).read_text(encoding="utf-8")
            parts.append(f"\n### {rel}\n```\n{content}\n```")

    commits = log(repo_path, 10)
    if commits:
        parts.append("\n## Git History (last 10)")
        parts.extend(commits)

    if last_validation:
        parts.append("\n## Last Validation Output")
        for result in last_validation:
            status = "PASS" if result.passed else "FAIL"
            parts.append(f"[{status}] {result.name}\n{result.output.strip()}")

    if progress.strip():
        parts.append("\n## Progress Notes")
        parts.append(progress.strip())

    return "\n".join(parts)
