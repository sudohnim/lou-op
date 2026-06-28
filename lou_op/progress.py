"""The progress/learnings file: Ralph's memory between fresh iterations.

Each Ralph iteration starts with a clean context, so iteration N has no innate
memory of what N-1 discovered. ``.lou-op/progress.md`` carries that forward:
appended learnings plus a consolidated "Codebase Patterns" section.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

PROGRESS_REL = Path(".lou-op") / "progress.md"

_HEADER = "# lou-op progress log\n\n## Codebase Patterns\n\n"


def progress_path(repo_path: Path) -> Path:
    return repo_path / PROGRESS_REL


def read_progress(repo_path: Path) -> str:
    path = progress_path(repo_path)
    if not path.exists():
        return ""
    return path.read_text(encoding="utf-8")


def append_progress(
    repo_path: Path, task_name: str, summary: str, iteration: int
) -> None:
    """Append one iteration's summary to the progress log."""
    path = progress_path(repo_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    if not path.exists():
        path.write_text(_HEADER, encoding="utf-8")
    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%SZ")
    entry = (
        f"\n## {stamp} — {task_name} (iteration {iteration})\n"
        f"{summary.strip()}\n---\n"
    )
    with path.open("a", encoding="utf-8") as handle:
        handle.write(entry)
