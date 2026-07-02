# lou-op
<img width="172" height="400" alt="image" src="https://github.com/user-attachments/assets/26d67ab9-8367-45ca-ab15-bf8f21e732e0" />

Async code generation via Ralph loops. Hand it a task list and a backend; it
iterates an LLM — read state → generate → validate → **commit every iteration**
— until the tests pass and the judge says the goal was met. Clean git history,
one commit per iteration. Named after Lou Wiggum: methodical, persistent, gets
the job done through iteration.

---

## Mental model

| Scenario | Tool |
|---|---|
| Interactive debugging, design, exploration | **Claude Code** (this session) |
| Single task, Claude CLI, you watch the terminal | **Chief** |
| Multi-task, async, swap in any model, check back later | **lou-op** |

Lou-op's value: **fire-and-forget + cheap models + audit trail.** Chief's
agent-cli pattern is one of three execution backends inside lou-op; the others
let you point at any OpenRouter model without needing a CLI installed.

---

## Quick start (no API keys)

```bash
pip install -e ".[dev]"        # or: uv pip install -e ".[dev]"
python -m lou_op.cli run tasks.example.yaml
```

The default **mock** backend is deterministic and needs no keys, so the whole
loop runs offline. Writes a tiny `calc` project into `.lou-op-jobs/<job_id>/`,
committing once per iteration on branch `lou-op/job-<job_id>`.

---

## Backends

Selected with `--backend` (CLI) or the `backend` field (API).

| Backend | Who writes files | Needs |
|---|---|---|
| `mock` | scripted, deterministic | nothing |
| `agent-cli` | a coding-agent CLI (`claude`, `codex`) writes files directly — no format-parse failures | CLI installed + authed |
| `raw-api` | lou-op parses `<<<FILE>>>` blocks from model output; optional SLM extractor cleans output first | `OPENROUTER_API_KEY` |

```bash
# run against DeepSeek via OpenRouter
LOU_MODEL_ID=deepseek/deepseek-coder \
OPENROUTER_API_KEY=sk-... \
python -m lou_op.cli run tasks.example.yaml --backend raw-api

# run with Claude CLI (chief pattern)
python -m lou_op.cli run tasks.example.yaml --backend agent-cli
```

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
    lint: true      # also runs black/isort/flake8/mypy
    judge: true     # LLM quality gate: "was the goal *actually* met?"
    max_iterations: 5
    status: pending  # written back as the job runs — crash-safe resume
```

### Task fields

| Field | Default | Description |
|---|---|---|
| `name` | required | Task identifier, used in commit messages |
| `description` | `""` | Shown to the agent each iteration |
| `success_criteria` | `[]` | Shell commands run in the repo; all must exit 0 |
| `lint` | `false` | Run black/isort/flake8/mypy in addition to criteria |
| `judge` | `false` | Add LLM-as-judge quality gate (see below) |
| `max_iterations` | `5` | Cap on loop iterations for this task |
| `status` | `pending` | Written back: `pending`/`in_progress`/`passed`/`failed` |

Tasks run **strictly linear**. `status` is written back after each task so a
crashed job resumes at the first unfinished task.

---

## LLM-as-judge

When `judge: true`, after each iteration's test commands pass, lou-op makes one
additional LLM call that reads the repo and asks:

> "Was the goal *actually* accomplished — or did the agent just game the tests?"

The judge returns `PASS` or `FAIL: <reason>`. On `FAIL`, the reason is fed back
into the next iteration's context so the agent knows what was missed.

This catches the canonical failure mode: the agent writes a trivially-passing
test (`def test_add(): pass`) without actually implementing the feature.

**Requires** `OPENROUTER_API_KEY`. Uses the same `LOU_MODEL_ID` model as the
raw-api backend. Judge API errors default to `PASS` so they never block a
working loop.

---

## Workspace types

| Type | When to use |
|---|---|
| `git` (default) | Code projects — clone, branch, commit, push |
| `null` | Data pipelines, analysis — just a directory, no git overhead |

```bash
python -m lou_op.cli run tasks.example.yaml --workspace null
```

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
    "backend": "raw-api",
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
| `LOU_BACKEND` | `mock` | Default backend (`mock`/`agent-cli`/`raw-api`) |
| `LOU_AGENT_PROVIDER` | `claude` | Agent provider (`claude`/`codex`) |
| `LOU_AGENT_CLI_PATH` | `claude` | Path to agent CLI binary |
| `LOU_MODEL_ID` | `z-ai/glm-4.6` | Model for raw-api backend and judge calls |
| `OPENROUTER_API_KEY` | — | Required for `raw-api` backend and judge |
| `LOU_JOBS_DIR` | `.lou-op-jobs` | Where job repos are cloned/created |
| `LOU_CONTEXT_BUDGET` | `100000` | Max chars of code fed to model per iteration |
| `LOU_INFERENCE_TIMEOUT` | `300` | Per-iteration inference timeout (seconds) |
| `LOU_SILENCE_TIMEOUT` | `300` | Watchdog: kill agent after N seconds silence |

---

## How the loop works (Ralph loop)

Each iteration for each task:

1. **Read state** — token-budgeted repo files + git log + last validation output + `.lou-op/progress.md`
2. **Generate** — backend writes files into the repo
3. **Validate** — run `success_criteria` commands + lint (if `lint: true`) + judge (if `judge: true`)
4. **Commit** — always commits, even on failure — one commit per iteration
5. **Append progress** — learnings written to `.lou-op/progress.md`, fed back next iteration
6. **Loop** — until all validators pass or `max_iterations` reached

The progress file is how iteration N benefits from what N-1 learned. Since each
Ralph iteration starts with a fresh LLM context, the file is the only persistent
memory between calls.

---

## Recommended first test

**CSV statistics tool** — bounded, verifiable, a cheap model needs 2–3 iterations:

```yaml
# tasks.yaml
tasks:
  - name: "CSV stats"
    description: |
      Create stats.py with compute_stats(path: str) -> dict that reads a CSV
      and returns {col: {mean, median, mode, std}} for each numeric column.
      Handle missing values (skip NaN). Non-numeric columns are ignored.
      Create tests/test_stats.py with a fixture CSV covering: normal values,
      NaN cells, non-numeric columns, and an empty numeric column.
    success_criteria:
      - "python3 -m pytest tests/ -q"
    judge: true
    max_iterations: 5
```

```bash
LOU_MODEL_ID=deepseek/deepseek-coder \
OPENROUTER_API_KEY=sk-... \
python -m lou_op.cli run tasks.yaml --backend raw-api --project-name csv-stats
```

Then inspect the job repo under `.lou-op-jobs/` — each git commit is one
iteration, message format: `CSV stats: iteration 2 [✓]`.

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
subprocesses** in the job's working directory. No sandbox in the MVP — only run
task lists and backends you trust. Docker/Modal isolation is on the roadmap.

The `agent-cli` backend uses `--allowedTools Read,Write,Edit,Bash,Glob,Grep,LS`
and `--strict-mcp-config` so the Claude agent operates hermetically (no MCP
servers, no external calls beyond the allowlist).
