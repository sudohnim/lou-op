# lou-op

<img width="172" height="400" alt="image" src="https://github.com/user-attachments/assets/26d67ab9-8367-45ca-ab15-bf8f21e732e0" />

Async, fire-and-forget code generation via Ralph loops. Hand it a PRD (or a task
list) and a backend; it iterates a model — read state → generate → **guard** →
validate → **commit every iteration** — until the tests pass. Clean git history,
one commit per iteration, and the model's claims are never trusted: **the
validators are the gate.**

Built to run **near-frontier open-weight models self-hosted** (GLM / Qwen /
DeepSeek on your own vLLM, on Modal, or via OpenRouter) with Claude-tier
reliability — so a client's code never has to leave your infrastructure.

Named after Lou, Chief Wiggum's lieutenant.

---

## Install

```bash
# global CLI (recommended) — editable, tracks the repo
uv tool install --editable .
lou-op --help

# or inside a project venv
uv pip install -e ".[dev]"
python -m lou_op.cli --help
```

> The `lou-op` command is the entry point everywhere below. If you change
> dependencies (not just code), re-run `uv tool install --editable . --force`.

---

## Quick start (no API keys)

```bash
lou-op run tasks.example.yaml --backend mock
```

The **mock** backend is deterministic and needs no keys, so the whole loop runs
offline — it writes a tiny project into `.lou-op-jobs/<job_id>/`, committing once
per iteration on branch `lou-op/job-<id>`.

Subcommands: `run` · `bench` (qualify a model) · `ping` (smoke-test an endpoint)
· `plan` (LLM-decompose a coarse task) · `reset` (mark tasks pending).
Global flags: `--log-level`, `--json-logs`.

---

## The PRD front door

The primary interface is a plain-English PRD. lou-op decomposes it into a task
graph, **writes the acceptance test for each task first**, freezes those tests,
and scopes each implementer to its own file.

```bash
lou-op run ~/my-project/PRD.md          # decompose + review the generated specs
lou-op run ~/my-project/PRD.md --yes    # decompose and build immediately
```

- The generated specs are written to disk and shown for review; re-run with
  `--yes` (or after editing them) to build. This is the **verifier-independence
  checkpoint**: you (or a stronger `LOU_SPEC_MODEL`) own the exam, not the
  implementer.
- The decomposition is cached in `.lou-op/tasks.json`; delete it to force a
  re-plan.
- A hand-written `tasks.yaml` is the escape hatch into the same task graph.

---

## Architecture — one domain, three ports, one working tree

The core is a **pure domain** (no I/O) that talks to the world through three
ports, each with a real adapter and a deterministic fake. The layering is
enforced by a test (`tests/test_import_boundaries.py`) — the domain may import
nothing outward.

```
interfaces   cli · api                         (argparse, FastAPI)
   │
domain       iteration · graph · scope · verification     ← pure, no I/O
   │
ports        Workspace · Provider · Store       ← interfaces the domain sees
   │
adapters     host/docker/modal · openai-compat · sqlite   ← real implementations
```

**Domain**
- `iteration` — a preemptible state machine: `GENERATE → GUARD → VALIDATE →
  COMMIT → decide`. An interrupt is legal from any state (deadlines don't wait
  for a boundary).
- `graph` — pure dependency scheduling: `ready()`, cycle/failed-dep detection,
  a `schedule()` step that's a pure function.
- `scope` — the write boundary, **fail-closed**: strict mode with nothing to
  infer refuses to run rather than run unbounded.
- `verification` — the gate as a typed object: `provenance` (an
  implementer-authored gate can't be authoritative), `frozen`, and an
  **anti-vacuous** precondition (a spec that passes with no implementation is
  invalid).

**Ports**
- `Workspace` — owns the working tree and is the **only** thing that reads,
  writes, or executes. Path-jailing lives here, once. `exec` hard-cancels on a
  deadline (kills the process group / container, never a cooperative flag).
- `Provider` — all model inference; every completion carries token usage + cost,
  so budgets and accounting are structural. Retries/timeouts centralized.
- `Store` — a durable, event-sourced log. `RunState` is a fold over versioned
  events; git commits, `progress.md`, and the audit trail are projections.

**Invariants** (asserted by the test suite): one tree · gate coherence ·
state-is-the-log · verifier independence · accounted inference · dependency
direction · no bypass · bounded termination.

---

## Backends & capability modes

Selected with `--backend` (CLI), the `backend` field (API), or `LOU_BACKEND`.

| Backend | Who writes files | Needs |
|---|---|---|
| `mock` | scripted, deterministic | nothing |
| `native` | **lou-op's own agent loop** against any OpenAI-compatible endpoint | an endpoint |
| `agent-cli` | a coding-agent CLI (`claude`, `codex`) writes files with its own tools | CLI installed + authed |

The **native** backend has two capability modes, chosen by the endpoint:

- **tools** (default) — the model emits OpenAI `tool_calls`
  (`read_file`/`write_file`/`edit_file`/`bash`/`list_dir`), executed through the
  Workspace (path-jailed, sandboxable, audited).
- **text** — a file-protocol fallback for endpoints that don't emit reliable
  tool-calls (e.g. vLLM without `--enable-auto-tool-choice`). Selected as
  `--backend raw-api`; it folds the old raw-api path into the one agent and,
  unlike before, writes **through the Workspace** so it inherits the jail.

```bash
# native loop against a local model — code never leaves the machine
LOU_OPENROUTER_BASE_URL=http://localhost:8000/v1 \
LOU_MODEL_ID=qwen3-coder OPENROUTER_API_KEY=local \
lou-op run tasks.yaml --backend native

# native loop via OpenRouter
LOU_MODEL_ID=z-ai/glm-4.6 OPENROUTER_API_KEY=sk-... \
lou-op run tasks.yaml --backend native

# delegated Claude CLI, pinned to the cheapest model
LOU_AGENT_MODEL=haiku lou-op run tasks.yaml --backend agent-cli
```

> **Isolation note.** A native agent's only egress is its `Provider`. A
> **delegated CLI is second-class for isolation** — it must reach a third-party
> vendor, so a sandbox can contain its filesystem and exec but cannot close that
> vendor egress. Use `native` when the data must not leave your infrastructure.

---

## Tasks file (`tasks.yaml`)

```yaml
tasks:
  - name: "CSV stats tool"
    description: |
      Implement stats.py that reads a CSV and returns mean/median/mode/std for
      each numeric column. Handle NaN/empty values gracefully.
    success_criteria:
      - "python3 -m pytest tests/ -q"
    protected_files: ["tests/**"]   # spec files: restored before every validation
    allowed_paths: ["stats.py"]     # writes outside these globs are reverted
    lint: true
    judge: true
    max_iterations: 5
    depends_on: []
    status: pending                 # written back as the job runs → crash-safe
```

| Field | Default | Description |
|---|---|---|
| `name` | required | Task identifier; used in commit messages |
| `description` | `""` | Shown to the agent each iteration |
| `success_criteria` | `[]` | Shell commands run in the workspace; all must exit 0 |
| `protected_files` | `[]` | Globs snapshotted at task start, restored before every validation — the model can't weaken the spec |
| `allowed_paths` | `[]` | If set, changes outside these globs are reverted (build artifacts + `.lou-op/` are exempt) |
| `lint` | `false` | Run formatters/linters in addition to criteria |
| `judge` | `false` | Consistency judge before each iteration (advisory; can abort, never passes a run) |
| `max_iterations` | `5` | Loop cap for this task |
| `depends_on` | `[]` | Task names that must be `passed` first; scheduler errors on cycles / failed deps |
| `status` | `pending` | Written back: `pending`/`in_progress`/`passed`/`failed` |

---

## Anti-gaming guards

The model's done-signal is advisory; the validators are the gate. Cheating is
structurally prevented, not merely discouraged:

1. **Vacuous-spec pre-flight** — validators run *before* iteration 1. Already
   green means the task is done (resume) or the spec can't fail — either way the
   model is never called.
2. **Protected files** — spec/test files are restored before every validation.
   Rewriting the exam doesn't pass it.
3. **Scope enforcement** — out-of-scope writes are reverted before validation
   (fail-closed under `--strict-scope`).
4. **Verifier independence** — an implementer-authored gate can't be marked
   authoritative; PRD specs are frozen and their authorship recorded.

Plus loop-level safeties: a no-op short-circuit (nothing written + tests failing
+ no done claim → stop) and false-done correction (claims done while red → a
correction is injected next iteration).

---

## Sandbox runtimes

Where model-influenced commands (the `bash` tool, `success_criteria`) execute.
Egress is **deny by default**.

| `--runtime` | Where commands run |
|---|---|
| `host` (default) | local process, in its own killable process group |
| `docker` | per-job container, `--cap-drop ALL`, non-root, no network |
| `modal` | a Modal sandbox; the repo is shipped in/out transparently per exec |

```bash
lou-op run tasks.yaml --backend native --runtime docker --strict-scope
lou-op run tasks.yaml --backend native --runtime docker --sandbox-network   # opt in to egress
```

All three are the same `Workspace` port, so guards, the agent's tools, and the
validators always operate on **one tree** — a sandbox validator can never grade a
state the guards didn't produce.

---

## Cost, token & time budgets

```bash
export LOU_MAX_JOB_TOKENS=200000          # hard token ceiling per job
export LOU_MAX_COST_USD=5                  # hard USD ceiling per job
export LOU_PRICE_IN_PER_MTOK=0.60          # your model's real prices
export LOU_PRICE_OUT_PER_MTOK=2.20
```

A breach aborts the job cleanly and logs a `budget_exceeded` event with tokens
and dollars. The job also has a wall-clock ceiling (`timeout_seconds`), enforced
by real process/container kill — a runaway command can't outlive its deadline.

---

## Durable state & resume

Every state change is an event appended to a SQLite event log
(`<jobs_dir>/events.db`). `RunState` is rebuilt by folding events, checkpointed
by periodic snapshots so resume cost is bounded, not O(history). Kill the process
mid-run and restart: the pure scheduler resumes at the first unfinished task with
no duplicated work. `progress.md`, git, and `.lou-op/audit.jsonl` are projections
of that log.

---

## Structured logging

Logging is [structlog](https://www.structlog.org) with contextvars — `job_id`,
`task`, and `iteration` are **bound once** and attached to every line
automatically (no callback threading through the layers).

```bash
lou-op run tasks.yaml --log-level debug      # more detail
lou-op run tasks.yaml --json-logs            # machine-readable JSON
LOU_LOG_LEVEL=debug LOU_LOG_JSON=1 lou-op run tasks.yaml
```

The live stream (CLI console, and the API `/logs` SSE endpoint) is driven off the
same events.

---

## Configuration & `.env`

`.env` is read from the **project you're running**, not from lou-op's own
directory. Precedence, highest → lowest:

```
real shell env  >  <project>/.env  >  ./.env  >  lou-op's own .env
```

So `lou-op run ~/proj/PRD.md` picks up `~/proj/.env`, while a real shell variable
still wins over any file.

### Environment variables

| Variable | Default | Description |
|---|---|---|
| `LOU_BACKEND` | `mock` | Default backend (`mock`/`native`/`agent-cli`) |
| `LOU_MODEL_ID` | `z-ai/glm-4.6` | Model for native + judge |
| `OPENROUTER_API_KEY` | — | Key for the endpoint (any non-empty value for local servers) |
| `LOU_OPENROUTER_BASE_URL` | OpenRouter | Any OpenAI-compatible endpoint (vLLM, Ollama, Modal, Baseten) |
| `LOU_AUTH_SCHEME` | `Bearer` | `Bearer` (OpenRouter/vLLM/Modal) or `Api-Key` (Baseten) |
| `LOU_SPEC_MODEL` | (= model) | Stronger model to author PRD specs (verifier independence) |
| `LOU_EXTRACTOR_MODEL_ID` | — | Optional SLM to clean up text-mode output |
| `LOU_AGENT_PROVIDER` | `claude` | Delegated CLI provider (`claude`/`codex`) |
| `LOU_AGENT_CLI_PATH` | `claude` | Path to the agent CLI binary |
| `LOU_AGENT_MODEL` | (CLI default) | Pin the agent-cli model, e.g. `haiku` |
| `LOU_RUNTIME` | `host` | `host`/`docker`/`modal` |
| `LOU_SANDBOX_NETWORK` | `off` | `on` to allow egress inside the sandbox |
| `LOU_STRICT_SCOPE` | `false` | Infer scope from the description when `allowed_paths` is empty |
| `LOU_MAX_PARALLEL` | `1` | Concurrent dependency-satisfied tasks (needs `workspace_type: null`) |
| `LOU_MAX_JOB_TOKENS` | `0` | Hard token ceiling per job (0 = unlimited) |
| `LOU_MAX_COST_USD` | `0` | Hard USD ceiling per job (0 = unlimited) |
| `LOU_PRICE_IN_PER_MTOK` / `LOU_PRICE_OUT_PER_MTOK` | `0` | Prices for USD accounting |
| `LOU_NATIVE_MAX_TURNS` | `40` | Native: max tool-call turns per iteration |
| `LOU_NATIVE_WALL_TIMEOUT` | `1800` | Native: wall-clock seconds per iteration |
| `LOU_CONTEXT_BUDGET` | `100000` | Max chars of code fed to the model (text mode) |
| `LOU_INFERENCE_TIMEOUT` | `300` | Per-request inference timeout (seconds) |
| `LOU_JOBS_DIR` | `.lou-op-jobs` | Where job repos + the event DB live |
| `LOU_LOG_LEVEL` / `LOU_LOG_JSON` | `info` / off | Logging verbosity / JSON output |

---

## Bench: qualify a model before trusting it

```bash
lou-op bench tasks.yaml --backend native --runs 3
```

Runs each task N times from clean state and reports pass rate + mean iterations
(the iteration-0 pre-flight is excluded), using the **same** runtime/scope config
a real run would — so the number is honest. Answers "is this model good enough
for this repo" before you hand it real work.

---

## API

```bash
uvicorn lou_op.api:app
```

```
POST /generate          → {job_id, status_url, git_branch}
GET  /status/{job_id}   → {status, current_task, completed_tasks, error}
GET  /results/{job_id}  → {git_branch, commit_log, completed_tasks, error}
GET  /logs/{job_id}     → SSE stream of the job's structured log lines
```

---

## Spec/impl split (the reliability pattern)

The highest-leverage practice: **don't ask the implementing model to write both
the tests and the code.** Seed (or generate with a stronger model) the test,
protect it, and scope the implementer to the implementation. The PRD front door
does exactly this automatically; by hand:

```yaml
tasks:
  - name: "KV store"
    description: "Implement store.py. The spec is tests/test_store.py — read it first."
    success_criteria: ["python3 -m pytest tests/ -q"]
    protected_files: ["tests/test_store.py"]
    allowed_paths: ["store.py"]
```

---

## Development

```bash
uv pip install -e ".[dev]"
pytest                                   # the suite is integration-first
python -m black lou_op tests && python -m flake8 lou_op tests
```

Testing is weighted toward **run-level integration tests** against a
deterministic fake `Provider` and a real temp-git `Workspace` — the invariants
are asserted as outcomes, because the bugs worth catching are composition bugs a
mocked unit test hides. Adversarial path-jail tests live at the `Workspace`; the
real model is never a CI gate.

---

## ⚠️ Security / trust

lou-op runs **model-written code and your `success_criteria` as subprocesses**.
Contained in the sandbox (`--runtime docker`/`modal`): the native `bash` tool and
the validator commands. **Not** contained: the native file tools (path-jailed to
the tree but host-side), and the `agent-cli` backend (a vendor CLI that phones
home). Default to `--runtime docker` and a task list you trust; use `native` for
data that must not leave your machine.
