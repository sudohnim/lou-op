"""The progress/scratchpad file: the agent's working memory between iterations.

Each iteration starts with a clean context. ``.lou-op/progress.md`` carries
state forward. The agent **rewrites** this file every iteration (not appends),
keeping it under ~1500 tokens. Git preserves the full history per commit, so
nothing is lost — old scratchpads are always recoverable via ``git log -p``.
"""

from __future__ import annotations

import re
from pathlib import Path

PROGRESS_REL = Path(".lou-op") / "progress.md"


def progress_path(repo_path: Path) -> Path:
    return repo_path / PROGRESS_REL


def read_progress(repo_path: Path) -> str:
    path = progress_path(repo_path)
    if not path.exists():
        return ""
    return path.read_text(encoding="utf-8")


def trim_progress(text: str, max_entries: int = 5) -> str:
    """Keep only the last max_entries iterations, preserving Codebase Patterns if present.

    If the text starts with a ## Codebase Patterns section, preserve it verbatim
    at the top (everything until the first ## Iteration header). Keep only the
    last max_entries ## Iteration sections. Text already within the limit is
    returned unchanged (modulo leading/trailing whitespace).
    """
    if not text.strip():
        return ""

    # Line-anchored header matches — a file that STARTS with "## Iteration"
    # must treat it as an iteration, not as pinned preamble (the old
    # "\n## Iteration" split silently pinned iteration 1 forever).
    headers = list(re.finditer(r"^## Iteration\b", text, flags=re.MULTILINE))
    if not headers:
        return text.strip()

    patterns_section = text[: headers[0].start()].rstrip()

    iterations = []
    for idx, match in enumerate(headers):
        end = headers[idx + 1].start() if idx + 1 < len(headers) else len(text)
        iterations.append("\n" + text[match.start() : end].rstrip())

    # Keep only the last max_entries iterations
    kept_iterations = iterations[-max_entries:] if iterations else []

    # Build the result
    if patterns_section and kept_iterations:
        result = patterns_section + "\n" + "\n".join(kept_iterations)
    elif kept_iterations:
        result = "\n".join(kept_iterations)
    else:
        result = patterns_section if patterns_section else ""

    return result.strip()


def write_scratchpad(repo_path: Path, content: str) -> None:
    """Overwrite progress.md with the agent's latest scratchpad (O(1) context)."""
    path = progress_path(repo_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content.strip() + "\n", encoding="utf-8")
