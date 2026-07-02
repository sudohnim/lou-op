"""Prompt templates fed to backends."""

from __future__ import annotations

from .models import Task
from .protocol import DONE_SENTINEL

_BASE = """\
You are an autonomous coding agent. Write source files to make all success criteria pass.
Do not explain your reasoning. Do not ask questions. Output code files only.

{state}
"""

_RAW_API_TAIL = r"""
## Output Format (REQUIRED)

Write each file as a markdown code block. The FIRST LINE inside each block MUST be a comment with the exact filename.

```python
# store.py
# complete file contents here
```

```python
# tests/test_store.py
# complete test file contents here
```

Rules:
- Write ALL required files in a single response
- FIRST LINE of each code block must be: # filename  (e.g. # store.py)
- No prose or explanations outside the code blocks
- Write complete files — do not truncate or summarize

"""

_RAW_API_STOP = "When ALL success criteria pass, output on its own line: {sentinel}\n"


def build_prompt(task: Task, state: str, *, raw_api: bool) -> str:
    """Render the iteration prompt."""
    prompt = _BASE.format(state=state, sentinel=DONE_SENTINEL)
    if raw_api:
        prompt += _RAW_API_TAIL
        prompt += _RAW_API_STOP.format(sentinel=DONE_SENTINEL)
    return prompt
