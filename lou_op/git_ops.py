"""Git operations via subprocess (no GitPython dependency).

lou-op owns commits so the "one commit per iteration" invariant holds across
every backend. Commits use ``--allow-empty`` so an iteration that changed
nothing still leaves a record.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import List, Optional, Tuple

from .exec import run_command

_AUTHOR_RE = re.compile(r"^(?P<name>.+?)\s*<(?P<email>[^>]+)>$")


def _git(repo_path: Path, *args: str, timeout: int = 120) -> str:
    result = run_command(["git", *args], repo_path, timeout=timeout)
    if not result.passed:
        raise RuntimeError(
            f"git {' '.join(args)} failed ({result.returncode}): "
            f"{result.stdout}{result.stderr}"
        )
    return result.stdout.strip()


def _parse_author(author: str) -> Tuple[str, str]:
    match = _AUTHOR_RE.match(author.strip())
    if not match:
        return author, "lou-op@sudohnim.dev"
    return match.group("name"), match.group("email")


def ensure_repo(repo_path: Path, remote: Optional[str] = None) -> None:
    """Make ``repo_path`` a git repo: clone ``remote`` if given, else init."""
    repo_path.parent.mkdir(parents=True, exist_ok=True)
    if (repo_path / ".git").exists():
        return
    if remote:
        run_command(
            ["git", "clone", remote, str(repo_path)],
            repo_path.parent,
            timeout=600,
        )
        return
    repo_path.mkdir(parents=True, exist_ok=True)
    _git(repo_path, "init")


def current_branch(repo_path: Path) -> str:
    return _git(repo_path, "rev-parse", "--abbrev-ref", "HEAD")


def checkout_branch(repo_path: Path, branch: str, base: Optional[str] = None) -> None:
    """Create-or-switch to ``branch``.

    If ``base`` is given and the branch does not yet exist, fork it from
    ``base`` (a clean base ref) instead of the current HEAD — so a new job does
    not stack on top of a previous job's committed output.
    """
    existing = run_command(["git", "rev-parse", "--verify", branch], repo_path)
    if existing.passed:
        _git(repo_path, "checkout", branch)
    elif base:
        _git(repo_path, "checkout", "-B", branch, base)
    else:
        _git(repo_path, "checkout", "-B", branch)


def _ref_exists(repo_path: Path, ref: str) -> bool:
    return run_command(
        ["git", "rev-parse", "--verify", "--quiet", ref], repo_path
    ).passed


def resolve_base_ref(repo_path: Path, configured: str = "") -> str:
    """The ref a new job branch should fork from — a stable base, never the tip
    of a previous job branch, so runs don't accumulate each other's output.

    Order: explicit config → ``origin/HEAD`` → ``main`` → ``master`` → current
    HEAD (the last is the safe no-op fallback for a fresh single-branch repo).
    """
    if configured and _ref_exists(repo_path, configured):
        return configured
    head = run_command(
        ["git", "symbolic-ref", "--quiet", "refs/remotes/origin/HEAD"], repo_path
    )
    if head.passed and head.stdout.strip():
        ref = head.stdout.strip().split("refs/remotes/", 1)[-1]  # e.g. origin/main
        if _ref_exists(repo_path, ref):
            return ref
    for name in ("main", "master"):
        if _ref_exists(repo_path, name):
            return name
    return "HEAD"


def commit_all(repo_path: Path, message: str, author: str) -> str:
    """Stage everything and commit (allowing empty); return the short SHA."""
    name, email = _parse_author(author)
    _git(repo_path, "add", "-A")
    run_command(
        [
            "git",
            "-c",
            f"user.name={name}",
            "-c",
            f"user.email={email}",
            "commit",
            "--allow-empty",
            "--author",
            f"{name} <{email}>",
            "-m",
            message,
        ],
        repo_path,
    )
    return current_commit(repo_path)


def current_commit(repo_path: Path) -> str:
    return _git(repo_path, "rev-parse", "--short", "HEAD")


def log(repo_path: Path, count: int = 10) -> List[str]:
    out = run_command(["git", "log", f"-{count}", "--oneline"], repo_path)
    if not out.passed:
        return []
    return [line for line in out.stdout.splitlines() if line.strip()]


def push(repo_path: Path, remote: str, branch: str) -> None:
    _git(repo_path, "push", remote, branch, timeout=600)


def revert_to(repo_path: Path, sha: str) -> None:
    """Hard-reset the working tree to ``sha``."""
    _git(repo_path, "reset", "--hard", sha)


_GITIGNORE = "\n".join(
    [
        "__pycache__/",
        "*.py[cod]",
        ".pytest_cache/",
        ".mypy_cache/",
        ".venv/",
        ".serena/",
        ".lou-op-jobs/",
        ".lou-op/metadata.json",
        "",
    ]
)

_GITIGNORE_APPEND = ".lou-op-jobs/\n"


def seed_gitignore(repo_path: Path) -> None:
    """Write a basic .gitignore if one doesn't exist; append .lou-op-jobs/ if missing."""
    path = repo_path / ".gitignore"
    if not path.exists():
        path.write_text(_GITIGNORE, encoding="utf-8")
    elif ".lou-op-jobs" not in path.read_text(encoding="utf-8"):
        with path.open("a", encoding="utf-8") as f:
            f.write(_GITIGNORE_APPEND)
