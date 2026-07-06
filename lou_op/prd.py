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

from typing import Callable, List, Optional

from .logutil import get_logger
from .models import Task

log = get_logger()

GenerateFn = Callable[[str], str]

_PRD_PROMPT = """\
You are a planner for an automated code-generation loop that works
test-first. Decompose this PRD into 2-6 small, independent tasks. For EACH
task, write the acceptance test FIRST — a test file that fails now and
passes only when the task is correctly implemented.

Use the test framework appropriate for the language and layer of the
stack described in the PRD:
- Python backend: pytest
- TypeScript/JavaScript frontend or backend: vitest
- Browser/E2E flows (auth, page rendering, form submission): playwright
- Go: go test
- Rust: cargo test

Prefer integration and E2E tests over isolated unit tests when the module
involves user-facing behavior (routing, rendering, form submission, auth
flows, API round-trips). Unit tests are appropriate for pure utility
functions (parsers, crypto helpers, data transforms).

Rules:
- Each task implements ONE cohesive module (may span a few related files);
  keep it small (1-3 iterations).
- The test file is the contract. Make it concrete and runnable.
- success_criteria is the shell command that runs that test file.
- impl_paths lists the file(s) or directories the implementer may write
  (never the test file itself).
- Order tasks so earlier ones don't depend on later ones.
- Match the language, framework, and toolchain specified in the PRD. Do
  NOT default to Python/pytest if the PRD specifies a different stack.
- If E2E tests require a running dev server, the success_criteria command
  should start the server, run tests, then shut it down in one shell
  command (e.g. using a background process and cleanup trap).

Respond with ONLY a JSON object (no prose, no markdown fences):
{{
  "tasks": [
    {{
      "name": "frontend-spa-and-routing",
      "description": "React SPA with Vite, BrowserRouter configured with basename from VITE_BASE_PATH, and a landing page with Google sign-in button.",
      "spec_path": "tests/e2e/landing.spec.ts",
      "spec_content": "import {{ test, expect }} from '@playwright/test';\\n\\ntest('landing page shows sign-in button', async ({{ page }}) => {{\\n  await page.goto('/');\\n  await expect(page.locator('button[data-testid=sign-in]')).toContainText('Sign in with Google');\\n}});",
      "impl_paths": ["src/", "index.html", "vite.config.ts"],
      "success_criteria": ["npx vite build && npx vite preview & SERVER_PID=$!; sleep 2; npx playwright test tests/e2e/landing.spec.ts --reporter=line; kill $SERVER_PID"]
    }}
  ]
}}

PRD:
---
{prd}
"""

# Shared project files that any task may legitimately need to modify
# (dependency manifests, lock files). Added to every task's allowed_paths
# so the guard doesn't revert `npm install` / `npm ci` side effects.
_SHARED_FILES = ["package.json", "package-lock.json"]


def _strip_fences(text: str) -> str:
    text = text.strip()
    if text.startswith("```"):
        lines = text.splitlines()
        end = len(lines) - 1 if lines[-1].strip() == "```" else len(lines)
        text = "\n".join(lines[1:end])
    return text.strip()


def _build_allowed_paths(impl_paths: List[str], spec_path: str) -> List[str]:
    """Construct the full allowed_paths list for a task.

    Includes:
    - The task's impl_paths (source files the model writes)
    - The spec test file path (so the guard doesn't delete+recreate it
      every iteration; _restore_protected still enforces content integrity)
    - Shared project files (package.json, package-lock.json) so the model
      can add dependencies without the guard reverting them
    """
    paths = list(impl_paths)
    if spec_path not in paths:
        paths.append(spec_path)
    for shared in _SHARED_FILES:
        if shared not in paths:
            paths.append(shared)
    return paths


def load_cached_tasks(repo_path: Path) -> Optional[List[Task]]:
    """Load cached task graph from a previous decomposition run.

    Returns None if no cache exists or if any referenced spec file is missing.
    """
    cache_file = repo_path / ".lou-op" / "tasks.json"
    if not cache_file.exists():
        return None

    try:
        data = json.loads(cache_file.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None

    tasks_data = data.get("tasks", [])
    if not tasks_data:
        return None

    tasks: List[Task] = []
    for spec in tasks_data:
        spec_path = spec["spec_path"]
        # Verify the spec file still exists on disk
        if not (repo_path / spec_path).exists():
            return None  # Cache is stale — spec file was deleted

        tasks.append(
            Task(
                name=spec["name"],
                description=spec.get("description", ""),
                success_criteria=spec.get("success_criteria")
                or [f"npx vitest run {spec_path}"],
                protected_files=[spec_path],
                allowed_paths=_build_allowed_paths(
                    list(spec.get("impl_paths", [])), spec_path
                ),
                depends_on=spec.get("depends_on", []),
                max_iterations=spec.get("max_iterations", 6),
            )
        )
    return tasks


def save_task_cache(specs: List[dict], repo_path: Path) -> None:
    """Persist task metadata so future runs can skip decomposition."""
    cache_dir = repo_path / ".lou-op"
    cache_dir.mkdir(parents=True, exist_ok=True)
    cache_file = cache_dir / "tasks.json"
    cache_file.write_text(
        json.dumps({"tasks": specs}, indent=2),
        encoding="utf-8",
    )


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
                or [f"npx vitest run {spec_path}"],
                # the exam is frozen: restored every iteration, never truly
                # editable by the model (protected_files enforces content)
                protected_files=[spec_path],
                allowed_paths=_build_allowed_paths(impl_paths, spec_path),
                depends_on=spec.get("depends_on", []),
                max_iterations=spec.get("max_iterations", 6),
            )
        )
    return tasks


def build_tasks_from_prd(
    prd_text: str, repo_path: Path, generate: GenerateFn
) -> List[Task]:
    """Full front door: PRD text → frozen-spec Task graph on disk.

    If a cached task graph exists (from a prior decomposition) and all
    referenced spec files are present, the cache is reused instead of
    re-decomposing. Delete .lou-op/tasks.json to force re-decomposition.
    """
    # Try to load cached tasks first
    cached = load_cached_tasks(repo_path)
    if cached is not None:
        log.info(
            "reusing cached task graph",
            phase="prd",
            tasks=len(cached),
            hint="delete .lou-op/tasks.json to force re-decomposition",
        )
        return cached

    # Fresh decomposition
    specs = decompose_prd(prd_text, generate)
    save_task_cache(specs, repo_path)
    return materialize_specs(specs, repo_path)
