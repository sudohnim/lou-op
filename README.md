# lou-op
<img width="172" height="400" alt="image" src="https://github.com/user-attachments/assets/26d67ab9-8367-45ca-ab15-bf8f21e732e0" />

Async code generation via Ralph loops.
Hand it a task list and a backend; it iterates an LLM — read state → generate → validate → **commit every iteration** — until the tests pass. Clean git history, one commit per iteration, and the model's claims are never trusted: validators are the gate.

Named after Lou, Chief Wiggum's lieutenant

---

## Mental model

| Scenario | Tool |
|---|---|
| Interactive debugging, design, exploration | **Claude Code** (this session) |
| Single task, Claude CLI, you watch the terminal | **Chief** |
| Multi-task, async, swap in any model, check back later | **lou-op** |

Lou-op's value: **fire-and-forget + cheap/local models + independent verification + audit trail.** Chief's agent-cli pattern is one of four execution backends; the `native` backend runs the agent loop itself against any OpenAI-compatible endpoint — including a fully local vLLM/Ollama server, so code never leaves your machine.

---

## Quick start (no API keys)

```bash
pip install -e ".[dev]"        # or: uv pip install -e ".[dev]"
python -m lou_op.cli run tasks.example.yaml
```

The default **mock** backend is deterministic and needs no keys, so the whole
loop runs offline. Writes a tiny `calc` project into `.lou-op-jobs/<job_id>/`,
committing once per iteration on branch `lou-op/job-<job_id>`.

CLI subcommands: `run` (execute a tasks file), `reset` (mark all tasks pending),
`plan` (LLM-decompose a coarse task into sub-tasks), `bench` (see below).

---

## Backends

Selected with `--backend` (CLI), the `backend` field (API), or `LOU_BACKEND`.

| Backend | Who writes files | Needs |
|---|---|---|
| `mock` | scripted, deterministic | nothing |
| `agent-cli` | a coding-agent CLI (`claude`, `codex`) writes files directly with its own tools | CLI installed + authed |
| `native` | **lou-op's own agent loop**: model emits OpenAI tool calls (`read_file`/`write_file`/`edit_file`/`bash`/`list_dir`), lou-op executes them path-jailed and feeds results back | any OpenAI-compatible endpoint |
| `raw-api` | one-shot: lou-op parses `<<<FILE>>>` blocks or `# filename`-headed markdown fences from model text | `OPENROUTER_API_KEY` |

```bash
# native loop against a local model — code never leaves the machine
LOU_OPENROUTER_BASE_URL=http://localhost:8000/v1 \
LOU_MODEL_ID=qwen3-coder OPENROUTER_API_KEY=local \
python -m lou_op.cli run tasks.yaml --backend native

# native loop via OpenRouter
LOU_MODEL_ID=z-ai/glm-4.6 OPENROUTER_API_KEY=sk-... \
python -m lou_op.cli run tasks.yaml --backend native

# Claude CLI (chief pattern), pinned to the cheapest model
LOU_AGENT_MODEL=haiku \
python -m lou_op.cli run tasks.yaml --backend agent-cli
```

Guidance from testing: `raw-api` is a fallback for narrow, single-file tasks
(blind one-shot generation — no tools, no self-correction). For real work use
`native` or `agent-cli`; the agent reads the failing test itself and fixes its
own mistakes within an iteration.

---

## Tasks file (`tasks.yaml`)

```yaml
tasks:
  - name: "CSV stats tool"
    description: |
      Implement stats.py that reads a CSV file and returns mean, median, mode,
      and std for each numeric column. Handle NaN/empty values gracefully.
    success_criteria:
      - "python3 -m pytest tests/ -q"
    protected_files: ["tests/**"]   # spec files the model must not touch
    allowed_paths: ["stats.py"]     # everything else it writes gets reverted
    lint: true
    judge: true
    max_iterations: 5
    depends_on: []
    status: pending  # written back as the job runs — crash-safe resume
```

### Task fields

| Field | Default | Description |
|---|---|---|
| `name` | required | Task identifier, used in commit messages |
| `description` | `""` | Shown to the agent each iteration |
| `success_criteria` | `[]` | Shell commands run in the repo; all must exit 0 |
| `protected_files` | `[]` | Globs snapshotted at task start and restored before every validation — the model cannot weaken the spec |
| `allowed_paths` | `[]` | If set, model changes outside these globs are reverted (`.lou-op/` exempt) |
| `lint` | `false` | Run black/isort/flake8/mypy in addition to criteria |
| `judge` | `false` | Consistency judge before each iteration (see below) |
| `max_iterations` | `5` | Cap on loop iterations for this task |
| `depends_on` | `[]` | Task names that must be `passed` first; the scheduler skips blocked tasks and errors on cycles or failed dependencies |
| `status` | `pending` | Written back: `pending`/`in_progress`/`passed`/`failed` |

`status` is written back after each task, so a crashed job resumes at the
first unfinished task — and the pre-flight check (below) makes re-running a
half-finished job free: already-green tasks are skipped without a model call.

---

## Anti-gaming guards

The model's done-signal is advisory; validators are the gate. Three guards
make cheating structurally impossible rather than merely discouraged:

1. **Vacuous-spec pre-flight** — validators run *before* iteration 1. Already
   green with zero work done means the task is finished (resume) or the spec
   can't fail (red-green violated) — either way the model is never called.
2. **Protected files** — spec/test files listed in `protected_files` are
   restored before every validation. Rewriting the exam doesn't pass it.
3. **Scope enforcement** — with `allowed_paths` set, out-of-scope writes are
   reverted before validation (untracked files deleted, tracked files reset).

Plus two loop-level safeties: a **no-op short circuit** (nothing written +
tests failing + no done claim → stop, needs human) and **false-done
correction** (model claims done while tests fail → warning injected into the
next iteration instead of stopping).

---

## Consistency judge

When `judge: true`, at the start of every iteration after the first, one LLM
call reads `.lou-op/progress.md`, the git log, and the source files and asks:
*does the codebase actually match the documented history?* Fabricated
progress, gamed tests, or undocumented changes raise `JudgeAbort` and stop the
job for manual intervention. Judge API errors are non-blocking (skip, never
stall a working loop). Requires `OPENROUTER_API_KEY`; uses `LOU_MODEL_ID`.

---

## Audit trail

The `native` backend writes `.lou-op/audit.jsonl` in the job repo: one JSON
line per tool call and result (`{"ts": ..., "event": "tool_call"|"tool_result",
"data": ...}`). This is the reviewable record of everything the model read,
wrote, and executed — the artifact that backs up "we ran this locally for data
custody."

---

## Bench: qualify a model before trusting it

```bash
python -m lou_op.cli bench tasks.yaml --backend native --runs 3
```

Runs each task N times from clean state (git-reset between runs) and prints
pass rate + mean iterations per task. Answers "is this model good enough for
this repo" empirically before you hand it a real job.

---

## Workspace types

| Type | When to use |
|---|---|
| `git` (default) | Code projects — clone, branch, commit, push |
| `null` | Data pipelines, analysis — just a directory, no git overhead |

Jobs run in `.lou-op-jobs/<job_id>/` by default, or in-place in an existing
repo via `project_path` (a `lou-op/job-<id>` branch keeps main untouched).

---

## API

```bash
uvicorn lou_op.api:app
```

```
POST /generate          → {job_id, status_url, git_branch}
GET  /status/{job_id}   → {status, current_task, completed_tasks, error}
GET  /results/{job_id}  → {git_branch, commit_log, completed_tasks, error}
GET  /logs/{job_id}     → SSE stream of agent output lines (text/event-stream)
```

### Submit a job

```bash
curl -X POST http://localhost:8000/generate \
  -H "Content-Type: application/json" \
  -d '{
    "project_name": "csv-stats",
    "backend": "native",
    "workspace_type": "git",
    "tasks": [{
      "name": "CSV stats tool",
      "description": "implement stats.py ...",
      "success_criteria": ["python3 -m pytest tests/ -q"],
      "judge": true,
      "max_iterations": 5
    }]
  }'

# stream live output
curl -N http://localhost:8000/logs/<job_id>

# poll status
curl http://localhost:8000/status/<job_id>
```

---

## Environment variables

| Variable | Default | Description |
|---|---|---|
| `LOU_BACKEND` | `mock` | Default backend (`mock`/`agent-cli`/`native`/`raw-api`) |
| `LOU_AGENT_PROVIDER` | `claude` | Agent provider (`claude`/`codex`) |
| `LOU_AGENT_CLI_PATH` | `claude` | Path to agent CLI binary |
| `LOU_AGENT_MODEL` | (CLI default) | Pin the agent-cli model, e.g. `haiku` |
| `LOU_MODEL_ID` | `z-ai/glm-4.6` | Model for native/raw-api backends and judge |
| `LOU_OPENROUTER_BASE_URL` | OpenRouter | Any OpenAI-compatible endpoint (vLLM, Ollama, Modal, ...) |
| `OPENROUTER_API_KEY` | — | Key for that endpoint (any non-empty value for local servers) |
| `LOU_NATIVE_MAX_TURNS` | `40` | Native backend: max tool-call turns per iteration |
| `LOU_NATIVE_WALL_TIMEOUT` | `1800` | Native backend: wall-clock seconds per iteration |
| `LOU_JOBS_DIR` | `.lou-op-jobs` | Where job repos are cloned/created |
| `LOU_CONTEXT_BUDGET` | `100000` | Max chars of code fed to model per iteration (raw-api) |
| `LOU_INFERENCE_TIMEOUT` | `300` | Per-request inference timeout (seconds) |
| `LOU_SILENCE_TIMEOUT` | `300` | Watchdog: kill agent after N seconds silence |

---

## How the loop works (Ralph loop)

Each iteration for each task:

0. **Pre-flight** (once per task) — run validators; already green → skip task
1. **Read state** — token-budgeted repo files + git log + last validation output + `.lou-op/progress.md`
2. **Generate** — backend writes files (directly with tools, or parsed from text)
3. **Guard** — revert out-of-scope changes, restore protected files
4. **Validate** — run `success_criteria` + lint (if `lint: true`); judge checks consistency at the start of iterations 2+
5. **Commit** — always commits, even on failure — one commit per iteration
6. **Append progress** — learnings written to `.lou-op/progress.md` (trimmed: a curated `## Codebase Patterns` section survives, only the last N iteration entries are kept), fed back next iteration
7. **Loop** — until all validators pass or `max_iterations` reached

Since each Ralph iteration starts with a fresh LLM context, the progress file
is the only persistent memory between calls — which is why it's curated, not
an unbounded log.

---

## Spec/impl split (recommended pattern)

The single highest-leverage reliability practice found in testing: **don't ask
the implementing model to write both the tests and the code.** Seed the test
file yourself (or generate it with a stronger model), protect it, and scope
the implementer to the implementation:

```yaml
tasks:
  - name: "KV store"
    description: "Implement store.py. The spec is tests/test_store.py — read it first."
    success_criteria: ["python3 -m pytest tests/ -q"]
    protected_files: ["tests/test_store.py"]
    allowed_paths: ["store.py"]
```

With this pattern a 7B local model passed tasks it failed 10/10 iterations on
when asked to write its own tests — and lou-op's own v0.3 features (audit
trail, dependency scheduling, progress trimming, bench) were built this way by
the cheapest Claude model against seeded specs.

---

## Development

```bash
./bin/lint.sh            # black + isort + flake8 + mypy
./bin/lint.sh --fix      # auto-format
pytest
git config core.hooksPath .githooks   # staged-file lint hook
```

---

## ⚠️ Security / trust

Lou-op runs **agent-written code and your `success_criteria` commands as
subprocesses** in the job's working directory. The native backend's file tools
are path-jailed to the repo and its `bash` tool is cwd-scoped with a timeout,
but there is **no OS-level sandbox in the MVP** — only run task lists and
backends you trust. Docker/Modal isolation is on the roadmap.

The `agent-cli` backend uses `--allowedTools Read,Write,Edit,Bash,Glob,Grep,LS`
and `--strict-mcp-config` so the Claude agent operates hermetically (no MCP
servers, no external calls beyond the allowlist).
