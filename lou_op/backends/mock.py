"""Deterministic backend for tests and key-free local runs.

By default it writes a tiny working ``calc`` project (impl + test) that makes
the example task's ``pytest`` succeed, then signals completion. Tests can
inject their own script: a mapping of task name -> files to write.
"""

from __future__ import annotations

from typing import Dict, List, Optional

from ..models import FileWrite, IterationContext, IterationOutput
from ..protocol import write_files
from .base import Backend

# Test lives at the repo root so pytest's prepend import mode puts the root on
# sys.path and ``import calc`` resolves.
_DEFAULT_CALC: List[FileWrite] = [
    FileWrite("calc.py", "def add(a, b):\n    return a + b\n"),
    FileWrite(
        "test_calc.py",
        "from calc import add\n\n\ndef test_add():\n    assert add(2, 3) == 5\n",
    ),
]


class MockBackend(Backend):
    name = "mock"
    include_code = True
    raw_api = False

    def __init__(self, script: Optional[Dict[str, List[FileWrite]]] = None) -> None:
        self.script = script or {}

    def run_iteration(self, ctx: IterationContext) -> IterationOutput:
        files = self.script.get(ctx.task.name, _DEFAULT_CALC)
        written = write_files(ctx.repo_path, files)
        summary = "Wrote: " + ", ".join(written) if written else "No changes"
        return IterationOutput(done=True, summary=summary, log=summary)
