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

    # Split by the iteration header pattern to extract components
    parts = re.split(r"(\n## Iteration)", text)

    # If fewer than 3 parts, there are no iterations
    if len(parts) < 3:
        return text.strip()

    patterns_section = parts[0].rstrip()

    # Reconstruct iterations from remaining parts (pairs of delimiter + content)
    iterations = []
    iteration_numbers = []
    for i in range(1, len(parts), 2):
        if i + 1 < len(parts):
            iteration_text = parts[i] + parts[i + 1]
            # Extract iteration number
            match = re.search(r"## Iteration (\d+)", iteration_text)
            if match:
                iteration_numbers.append(int(match.group(1)))
            iterations.append(iteration_text)

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
