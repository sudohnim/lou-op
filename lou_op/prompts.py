"""Prompt templates fed to backends (adapted from Chief's embed/prompt.txt)."""

from __future__ import annotations

from .models import Task
from .protocol import DONE_SENTINEL

_BASE = """\
You are an autonomous coding agent working on a software project.

{state}

## Instructions
1. Implement the current task above so every success criterion passes.
2. Run the quality checks the project requires.
3. Keep changes focused and follow existing code patterns.
4. Append a short "Learnings for future iterations" note to
   .lou-op/progress.md (create it if missing). Record reusable patterns and
   gotchas; future iterations start fresh and rely on this file.

## Stop Condition
Review EACH success criterion one by one and verify it is met.
Only if ALL criteria pass, output the exact token {sentinel} on its own line.
"""

_RAW_API_TAIL = """\

## Output Format
Return the full content of every file you create or change as blocks:

<<<FILE path/to/file>>>
...entire file content...
<<<END>>>

Emit one block per file. Do not abbreviate or use placeholders.
"""


def build_prompt(task: Task, state: str, *, raw_api: bool) -> str:
    """Render the iteration prompt. ``raw_api`` appends the file protocol."""
    prompt = _BASE.format(state=state, sentinel=DONE_SENTINEL)
    if raw_api:
        prompt += _RAW_API_TAIL
    return prompt
