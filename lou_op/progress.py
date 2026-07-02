"""The progress/scratchpad file: the agent's working memory between iterations.

Each iteration starts with a clean context. ``.lou-op/progress.md`` carries
state forward. The agent **rewrites** this file every iteration (not appends),
keeping it under ~1500 tokens. Git preserves the full history per commit, so
nothing is lost — old scratchpads are always recoverable via ``git log -p``.
"""

from __future__ import annotations

from pathlib import Path

PROGRESS_REL = Path(".lou-op") / "progress.md"


def progress_path(repo_path: Path) -> Path:
    return repo_path / PROGRESS_REL


def read_progress(repo_path: Path) -> str:
    path = progress_path(repo_path)
    if not path.exists():
        return ""
    return path.read_text(encoding="utf-8")


def write_scratchpad(repo_path: Path, content: str) -> None:
    """Overwrite progress.md with the agent's latest scratchpad (O(1) context)."""
    path = progress_path(repo_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content.strip() + "\n", encoding="utf-8")
