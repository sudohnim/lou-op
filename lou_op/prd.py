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

from .models import Task

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
- Order tasks so earlier ones don't depend on later ones.
- Match the language, framework, and toolchain specified in the PRD. Do
  NOT default to Python/pytest if the PRD specifies a different stack.
- For E2E/browser tests that need a running app, let the TEST RUNNER own the
  server lifecycle — never hand-roll it in the shell. E.g. Playwright's
  `webServer` in playwright.config, so the gate is just `npx playwright test`.
  Its command MUST rebuild from source then serve
  (e.g. "npm run build && npm run preview") — NEVER a bare "preview"/"serve"
  that reuses a stale dist/, or the gate passes against an old build. Do NOT
  write `serve & sleep N; test; kill`: the sleep races the build, and a failed
  build then looks like a test failure instead of a broken harness.
- Every success_criteria must be reproducible from a clean checkout: produce
  whatever it tests from CURRENT source inside the command, then test. Never
  test a pre-built artifact (dist/, target/, build/, .next/, out/) or a
  process started outside the command — a gate that reads stale state can go
  green while the committed source is wrong.
- Serve the E2E build at the ROOT path, and have the tests navigate from root
  (page.goto('/...')). A subpath deploy (e.g. /kuma) is a production
  build-config concern; if the E2E server base and the test navigation
  disagree, every route 404s and the gate can never pass.
- If the project has no build harness yet, the FIRST task should create it
  (config files, entry points, stub pages). Its success criterion is simply
  that the build or type-check succeeds.
- The build/scaffold gate MUST TYPE-CHECK or COMPILE, not merely bundle —
  bundlers (vite/esbuild/webpack) skip type errors, so a task that imports a
  symbol another task never exported still "builds", and the break only shows
  up at runtime. Use the type/compile checker for the stack: TS
  "tsc --noEmit", Go "go build ./...", Rust "cargo build", Python
  "mypy ." or an import smoke-test. This is what catches cross-task API drift
  (one task defines `parseX`, another imports `detectX`).
- If the PRD describes a multi-process app (e.g. a frontend PLUS a backend /
  worker / API server), emit a dedicated task for the RUN + DEPLOY HARNESS —
  the config and scripts that let a human boot every process together locally
  (wrangler.toml + `.dev.vars.example`, a compose file, a `dev:all` script) —
  AND an INTEGRATION task that verifies the cross-process CONTRACT.
- CRITICAL: the integration gate must verify that contract IN-PROCESS, using
  the framework's in-memory test harness — NOT by booting real servers, a
  browser, and a proxy. Use `unstable_dev` (Cloudflare Workers), supertest or a
  fetch against an in-process app (Node/Express), FastAPI/Flask `TestClient`,
  Rails integration tests, etc. Call the handler in memory and assert the
  contract (e.g. `POST /api/chat` with no auth cookie returns 401; the reply
  shape matches what the frontend client parses). This catches the real bug
  class — frontend and backend disagreeing on the request/response shape, or a
  route missing — WITHOUT the multi-service orchestration (ports, readiness
  URLs, build artifacts, headed browsers) that makes a gate slow, flaky, and
  effectively un-passable for the implementer. Booting the true wired runtime
  (does the proxy route, does wrangler serve) is a deploy concern the human
  validates by running the harness — it is NOT the integration gate.

Respond with ONLY a JSON object (no prose, no markdown fences). The two
examples below show a pure-unit task and an E2E task — note the E2E gate is a
bare `playwright test` (no `serve & sleep; kill`) and the spec navigates from
root; the server is owned by `webServer` in the playwright.config the
implementer writes:
{{
  "tasks": [
    {{
      "name": "slugify-util",
      "description": "Pure slugify helper.",
      "spec_path": "tests/slugify.test.ts",
      "spec_content": "import {{ describe, it, expect }} from 'vitest';\\nimport {{ slugify }} from '../src/slugify';\\n\\ndescribe('slugify', () => {{\\n  it('lowercases and dashes', () => {{\\n    expect(slugify('A B')).toBe('a-b');\\n  }});\\n}});",
      "success_criteria": ["npx vitest run tests/slugify.test.ts"]
    }},
    {{
      "name": "landing-page",
      "description": "Landing page with a sign-in button, served at root.",
      "spec_path": "tests/e2e/landing.spec.ts",
      "spec_content": "import {{ test, expect }} from '@playwright/test';\\n\\ntest('shows sign-in', async ({{ page }}) => {{\\n  await page.goto('/');\\n  await expect(page.getByTestId('sign-in')).toBeVisible();\\n}});",
      "success_criteria": ["npx playwright test tests/e2e/landing.spec.ts"]
    }}
  ]
}}

PRD:
---
{prd}
"""


def _strip_fences(text: str) -> str:
    text = text.strip()
    if text.startswith("```"):
        lines = text.splitlines()
        end = len(lines) - 1 if lines[-1].strip() == "```" else len(lines)
        text = "\n".join(lines[1:end])
    return text.strip()


def _task_from_spec(spec: dict) -> Task:
    """Build one frozen-spec Task from a decomposed spec dict.

    The only constraint on the implementer is the frozen exam: ``spec_path``
    is protected (restored every iteration, so the model can't edit its own
    grader). The model may write any other file — the gate judges the result,
    not a file-scope fence.
    """
    criteria = spec.get("success_criteria")
    if not criteria:
        raise ValueError(
            f"Task '{spec['name']}' has no success_criteria — "
            "the spec model must emit this field"
        )
    return Task(
        name=spec["name"],
        description=spec.get("description", ""),
        success_criteria=criteria,
        protected_files=[spec["spec_path"]],
        depends_on=spec.get("depends_on", []),
        max_iterations=spec.get("max_iterations", 6),
    )


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

        tasks.append(_task_from_spec(spec))
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


def decompose_prd(
    prd_text: str, generate: GenerateFn, *, save_dir: Optional[Path] = None
) -> List[dict]:
    """Ask the model to turn a PRD into task dicts with embedded specs.

    Decompositions are large (every spec file embedded as JSON), so a single
    malformed response is common. Retry once — LLMs usually emit clean JSON
    the second time — and on give-up dump the raw text under ``save_dir`` so
    the failure is inspectable, not a bare column offset. Truncation raises
    from ``generate`` (TruncatedResponseError) and is deliberately NOT retried:
    the same cap would just truncate again.
    """
    prompt = _PRD_PROMPT.format(prd=prd_text)
    last_raw = ""
    last_err: Optional[json.JSONDecodeError] = None
    for _ in range(2):
        last_raw = generate(prompt)
        try:
            data = json.loads(_strip_fences(last_raw))
            break
        except json.JSONDecodeError as exc:
            last_err = exc
    else:
        hint = ""
        if save_dir is not None:
            dump = Path(save_dir) / ".lou-op" / "decomposition_raw.txt"
            dump.parent.mkdir(parents=True, exist_ok=True)
            dump.write_text(last_raw, encoding="utf-8")
            hint = f" — raw response saved to {dump}"
        raise ValueError(f"spec model returned invalid JSON: {last_err}{hint}")

    tasks = data.get("tasks", [])
    if not tasks:
        raise ValueError("PRD decomposition produced no tasks")
    return tasks


def materialize_specs(specs: List[dict], repo_path: Path) -> List[Task]:
    """Write each generated spec to disk and freeze it into the Task.

    The spec file is written BEFORE the loop runs and listed in
    protected_files — so the loop restores it on every iteration and the
    implementer can never edit its own exam.
    """
    tasks: List[Task] = []
    for spec in specs:
        spec_path = spec["spec_path"]
        target = repo_path / spec_path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(spec["spec_content"], encoding="utf-8")
        tasks.append(_task_from_spec(spec))
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
        print(f"[prd] reusing cached task graph ({len(cached)} tasks)")
        print("[prd] delete .lou-op/tasks.json to force re-decomposition")
        return cached

    # Fresh decomposition
    specs = decompose_prd(prd_text, generate, save_dir=repo_path)
    save_task_cache(specs, repo_path)
    return materialize_specs(specs, repo_path)
