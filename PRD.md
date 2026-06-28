# lou-op: Async Code Generation via Ralph Loops

**Version:** 0.2 (Design locked, MVP in build)
**Last Updated:** June 28, 2026
**Status:** Implementing MVP

> Changelog vs v0.1: scoped MVP to a **local-first core** (Modal deferred);
> replaced the single raw-API loop with **one pluggable `Backend` interface**
> (agent-CLI default / mock / raw-API + optional SLM extractor); folded in
> patterns from [Chief](https://github.com/minicodemonkey/chief) (progress
> file, watchdog + retry, living-doc task state) and the lint stack from the
> `eidolon` project; corrected unreal model assumptions ("GLM-5.2"); replaced
> whole-codebase serialization with a token-budgeted file selection.

---

## Vision

An orchestration service that transforms a PRD into a working codebase through
iterative LLM-driven development (Ralph loops). Submit a task list, get back a
git repo with clean history — one commit per iteration. Fire-and-forget by
design; check progress asynchronously.

Named in homage to Lou Wiggum: persistent, methodical, gets the job done
through iteration.

---

## Problem Statement

- **Token bleed:** repetitive code-gen drains tokens fast in interactive tools.
- **Waiting tax:** developers babysit inference and context limits.
- **No fire-and-forget pattern:** no simple "submit tasks → check back later."
- **Iteration visibility:** existing tools hide what failed and why; lou-op
  keeps every iteration as a commit.

---

## Scope

### MVP (this build) — local-first core

Runs end-to-end on a single machine (an M1 MacBook Air) with **zero cloud spend
or API keys** via the default mock backend. Modal deployment becomes a thin
wrapper added later.

In scope:

- Ralph loop engine (read state → backend iteration → validate → commit).
- One pluggable `Backend` interface (see Backends).
- Git-based workflow, one commit per iteration, single evolving repo.
- Hand-written `tasks.yaml` with living-doc task state (resume on crash).
- Subprocess validation (test/lint commands) with timeouts.
- FastAPI endpoints + a local CLI runner.
- Progress/learnings file fed back each iteration.

Out of scope (deferred): Modal deploy, real hosted backends (Baseten/Ollama),
LLM task decomposition, WebSocket streaming, cost budgets, parallel tasks,
Docker sandbox, git worktrees.

---

## Core Architecture

### Stack

- **API:** FastAPI (local now; Modal wrapper later).
- **Orchestration:** Ralph Loop pattern (Python) — fresh context each
  iteration, state persisted between loops via git + a progress file.
- **State:** the git repo is the source of truth.
- **Execution:** subprocess runners for tests/linting, with watchdog timeouts.
- **Language:** Python 3.11+.

### Data flow

```
Client → POST /generate (tasks + project + backend)
  ↓
Background job (returns job_id)
  ↓
  ├─ ensure repo (git init for greenfield, or operate in place)
  ├─ branch: lou-op/job-{job_id}
  ├─ for each task (resume at first unfinished):
  │  ├─ Ralph loop, while not done and iter ≤ max_iterations:
  │  │  ├─ render state (budgeted files + git log + last validation + progress)
  │  │  ├─ backend.run_iteration(ctx)   # files land on disk
  │  │  ├─ run validators (test/lint commands; optional built-in lint)
  │  │  ├─ commit (every iteration, even on failure)
  │  │  ├─ append learnings to .lou-op/progress.md
  │  │  └─ done if all validators pass OR <lou-done/> sentinel
  │  └─ write task status back to tasks.yaml
  ├─ push to remote (optional)
  └─ status/results via polling endpoints
```

---

## Backends

lou-op exposes **one `Backend` interface** with three implementations. The loop
body is identical regardless of backend; only *who writes the files* differs.

```python
class Backend(ABC):
    def run_iteration(self, ctx: IterationContext) -> IterationOutput: ...
    # side effect: writes files into ctx.repo_path
    # returns: done flag + log + summary
```

### agent-CLI (default for real runs)

Spawns an existing coding-agent CLI (`claude`, `codex`, …) as a subprocess in
the work directory. The agent reads and writes files directly with its own
edit tools, so **there is no text-format-parsing failure mode**. lou-op
supervises: hands over the prompt, streams output, detects the `<lou-done/>`
sentinel, then runs validators and owns the commit.

- **Pro:** most reliable file editing; no fragile output protocol.
- **Con:** requires the agent CLI installed and authenticated.

### mock (default for tests)

Deterministic, scripted file writes. No keys, no network. Drives the full loop
and validators in CI so everything is testable offline.

### raw-API + optional SLM extractor (third path)

Calls a model API for text, then parses an explicit file protocol and writes
the files itself:

```
<<<FILE path/to/file.py>>>
...contents...
<<<END>>>
```

Because models are sloppy at hitting an exact format every time, this path
supports an **optional SLM extractor**: a cheap, small model re-reads the big
model's output and re-emits clean `<<<FILE>>>` blocks before parsing. The
extractor is a mitigation for this path only — the agent-CLI path avoids the
problem entirely.

- **Pro:** pure-API, works with any hosted model (OpenRouter, etc.).
- **Con:** brittle on malformed output (extractor reduces but cannot eliminate).

**Backend selected per request via the `backend` field. Default: mock.**

---

## Features

### 1. API + CLI

- `POST /generate` — submit tasks + metadata; returns
  `{job_id, status_url, git_branch}`; kicks off a background job.
- `GET /status/{job_id}` — `{status, current_task, completed_tasks, error}`.
- `GET /results/{job_id}` — `{git_branch, commit_log, summary}`.
- CLI: `python -m lou_op.cli run tasks.yaml [--backend ...]` for local runs.

### 2. Ralph loop (per task)

Read state → `backend.run_iteration` → run validators → **commit every
iteration** → append progress → check completion. Completion = all validators
pass **or** the model emits `<lou-done/>`. Each iteration wraps the backend
call in retry-with-backoff, guarded by a watchdog.

### 3. State serialization (token-budgeted)

Fed to the backend each iteration:

- **Files:** enumerated via git (respecting `.gitignore`), selected up to a
  **token budget** (default 100k) — not a whole-codebase dump. Files named in
  the task are prioritized. (agent-CLI reads the repo itself, so it gets a
  lighter prompt.)
- **Git history:** last 10 commits.
- **Last validation output:** stdout/stderr/exit codes.
- **Progress file:** `.lou-op/progress.md` learnings + Codebase Patterns.

### 4. Git-based workflow

- One commit per iteration (even when validators fail).
- Message: `{task_name}: iteration {n} [{status}]`.
- Branch per job: `lou-op/job-{job_id}`.
- Author: `lou-op <lou-op@sudohnim.dev>`.
- Single evolving repo (no worktrees) so the project builds up over iterations.
- Push to remote only when a remote and credentials are present.

### 5. Progress / learnings file (Ralph memory)

`.lou-op/progress.md` is appended each iteration with what was done and
"Learnings for future iterations," plus a consolidated "Codebase Patterns"
section. Since each Ralph iteration starts fresh, this file is how iteration N
benefits from what iteration N-1 discovered.

### 6. Validation

- **Command validators:** each `success_criteria` entry is a shell command run
  as a subprocess with a timeout; pass/fail + captured output feed the next
  iteration.
- **Built-in Python lint validator (optional, `lint: true`):** runs
  `black --check`, `isort --check`, `flake8`, `mypy` over the generated project
  — the same stack lou-op uses on itself.
- **Watchdog:** kill an iteration on silence/total timeout.

### 7. Task breakdown (hand-written)

```yaml
tasks:
  - name: "Auth Module"
    description: "Implement user authentication with JWT"
    success_criteria:
      - "pytest tests/auth/"
    lint: true
    max_iterations: 5
    status: pending          # written back: pending|in_progress|passed|failed
```

Tasks run sequentially. `status` is written back to the file after each task,
so a crashed/timed-out job resumes at the first unfinished task. (LLM-driven
decomposition is deferred to a later version.)

---

## Reliability

- **Living-doc task state** → crash/timeout resume.
- **Watchdog + retry-with-backoff** (3×, delays 0/5/15s) supersedes the old
  single-retry rule.
- **Max iterations per task** (default 5) prevents runaway loops.
- **Job timeout** (default 2h) aborts the job.
- Git is the durable state; every iteration is recoverable.

---

## Success Criteria

### Functional

- Submit tasks via API/CLI, receive async job id.
- Job completes with code + git history; each iteration a separate commit.
- Validators run and inform the next iteration.
- Pluggable backends (mock, agent-CLI, raw-API).
- Crash resume via living-doc task state.

### UX

- Status checkable via polling endpoint.
- Git history is clean, reviewable, one commit per iteration.
- Iteration count shows the model's problem-solving effort.
- Clear errors on failure.

---

## Technical Requirements

- **State:** budgeted file selection + git log + validation output + progress.
- **Context budget:** keep prompt < ~100k tokens.
- **File reading:** git-tracked + untracked-not-ignored (respects `.gitignore`).
- **Backends:** `mock` | `agent-cli` | `raw-api`; default `mock`.
- **Inference timeout:** configurable (default 300s/iteration).
- **Git:** init/branch/commit/push via subprocess (no GitPython dependency).

---

## Deployment

- **MVP:** local (CLI + `uvicorn lou_op.api:app`).
- **Language:** Python 3.11+.
- **Runtime deps:** FastAPI, Uvicorn, Pydantic, PyYAML, httpx.
- **Dev deps:** pytest, black, isort, flake8, mypy.
- **Later:** Modal serverless wrapper around the same core.

---

## Open Questions (carried forward)

1. **Git remote:** default target for pushes (GitHub PAT? local-only?).
2. **Inference fallback:** on poor quality — escalate model, reprompt, or fail?
3. **Monitoring:** which observability first — cost/job, iterations, quality?
4. **Privacy posture** when hosted backends land (Baseten vs local).

---

## Roadmap

- **v0.2 (now):** local-first core — loop, backends (mock/agent-CLI/raw-API),
  git workflow, validators, progress file, FastAPI + CLI.
- **v0.3:** Modal deploy wrapper; real hosted backends (OpenRouter/Baseten).
- **v1.0:** LLM task decomposition; streaming results; cost budgets + auto-halt.
- **v2:** parallel tasks; multi-model routing; interactive pause.

---

## Notes

- Named "lou-op" after Lou Wiggum — methodical, persistent, iterative.
- Ralph Loop: https://ghuntley.com/ralph/ — fresh context each iteration,
  state persisted between loops.
- Borrows agent-CLI orchestration, the progress file, watchdog/retry, and
  living-doc task state from Chief (https://github.com/minicodemonkey/chief);
  borrows the lint stack from the `eidolon` project.
