"""PRD front door: a markdown PRD becomes a task graph with frozen specs.

`lou-op run prd.md` decomposes the PRD into small tasks, and for each task
the spec-authoring model writes the TEST FIRST. Those tests are written to
disk and auto-protected before the impl loop starts — the implementer can
never edit its own exam (verifier independence, B2/B3).

The generated tasks are ordinary Task objects, so the whole existing loop
(guards, validators, runtime) applies unchanged. A tasks.yaml still works
as the escape hatch — this is an additional input, not a replacement.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Callable, List

from .models import Task

GenerateFn = Callable[[str], str]

_PRD_PROMPT = """\
You are a planner for an automated code-generation loop that works
test-first. Decompose this PRD into 2-6 small, independent tasks. For EACH
task, write the acceptance test FIRST — a pytest file that fails now and
passes only when the task is correctly implemented.

Rules:
- Each task implements ONE module/file; keep it small (1-3 iterations).
- The test file is the contract. Make it concrete and runnable.
- success_criteria is the shell command that runs that test file.
- impl_paths lists the file(s) the implementer may write (never the test).
- Order tasks so earlier ones don't depend on later ones.

PRD:
{prd}

Return ONLY valid JSON, no markdown fences:
{{
  "tasks": [
    {{
      "name": "short-kebab-name",
      "description": "what to implement and why",
      "spec_path": "tests/test_<thing>.py",
      "spec_content": "import ...\\n\\ndef test_...():\\n    ...",
      "impl_paths": ["<module>.py"],
      "success_criteria": ["python -m pytest tests/test_<thing>.py -q"]
    }}
  ]
}}
"""


def _strip_fences(text: str) -> str:
    text = text.strip()
    if text.startswith("```"):
        lines = text.splitlines()
        end = len(lines) - 1 if lines[-1].strip() == "```" else len(lines)
        text = "\n".join(lines[1:end])
    return text.strip()


def decompose_prd(prd_text: str, generate: GenerateFn) -> List[dict]:
    """Ask the model to turn a PRD into task dicts with embedded specs."""
    raw = generate(_PRD_PROMPT.format(prd=prd_text))
    data = json.loads(_strip_fences(raw))
    tasks = data.get("tasks", [])
    if not tasks:
        raise ValueError("PRD decomposition produced no tasks")
    return tasks


def materialize_specs(specs: List[dict], repo_path: Path) -> List[Task]:
    """Write each generated spec to disk and freeze it into the Task.

    The spec file is written BEFORE the loop runs and listed in
    protected_files — so the implementer's guard restores it on every
    iteration and out-of-scope enforcement keeps the model in impl_paths.
    """
    tasks: List[Task] = []
    for spec in specs:
        spec_path = spec["spec_path"]
        target = repo_path / spec_path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(spec["spec_content"], encoding="utf-8")

        impl_paths = list(spec.get("impl_paths", []))
        tasks.append(
            Task(
                name=spec["name"],
                description=spec.get("description", ""),
                success_criteria=spec.get("success_criteria")
                or [f"python -m pytest {spec_path} -q"],
                # the exam is frozen: restored every iteration, never in scope
                protected_files=[spec_path],
                allowed_paths=impl_paths,
                depends_on=spec.get("depends_on", []),
                max_iterations=spec.get("max_iterations", 6),
            )
        )
    return tasks


def build_tasks_from_prd(
    prd_text: str, repo_path: Path, generate: GenerateFn
) -> List[Task]:
    """Full front door: PRD text → frozen-spec Task graph on disk."""
    return materialize_specs(decompose_prd(prd_text, generate), repo_path)
