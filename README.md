# lou-op
<img width="172" height="400" alt="image" src="https://github.com/user-attachments/assets/26d67ab9-8367-45ca-ab15-bf8f21e732e0" />

[Ralph Loop](https://ghuntley.com/loop/) implementation asynchronously either locally or remotely.

Async code generation: hand it a task list, it iterates an LLM in a loop —
read state → generate → validate → **commit every iteration** — until the
tests pass. Clean git history, one commit per iteration. See [PRD.md](PRD.md).

## Quick start (no API keys)

```bash
pip install -e ".[dev]"        # or: uv pip install -e ".[dev]"
python -m lou_op.cli run tasks.example.yaml
```

The default **mock** backend is deterministic and needs no keys, so the whole
loop runs offline. It writes a tiny `calc` project into a fresh repo under
`.lou-op-jobs/<job_id>/`, committing once per iteration on branch
`lou-op/job-<job_id>`.

## Backends

Selected with `--backend` (CLI) or the `backend` field (API). One interface,
three engines:

| Backend | Who writes files | Needs |
|---|---|---|
| `mock` (default) | scripted, deterministic | nothing |
| `agent-cli` | a coding-agent CLI (`claude`/`codex`) writes them directly — no format-parse failures | that CLI installed + authed |
| `raw-api` | lou-op parses `<<<FILE>>>` blocks from model text (optional SLM extractor cleans output first) | `OPENROUTER_API_KEY` |

```bash
python -m lou_op.cli run tasks.example.yaml --backend agent-cli
```

Config is env-driven: `LOU_BACKEND`, `LOU_AGENT_PROVIDER`, `LOU_AGENT_CLI_PATH`,
`LOU_MODEL_ID`, `OPENROUTER_API_KEY`, `LOU_CONTEXT_BUDGET`, `LOU_JOBS_DIR`.

## API

```bash
uvicorn lou_op.api:app
# POST /generate  -> {job_id, status_url, git_branch}
# GET  /status/{job_id}
# GET  /results/{job_id}
```

## Tasks file

`success_criteria` entries are shell commands run in the generated repo; a task
passes when they all exit 0 (or the model emits `<lou-done/>`). `status` is
written back as the job runs, so a crashed job resumes at the first unfinished
task. See [tasks.example.yaml](tasks.example.yaml).

## Development

```bash
./bin/lint.sh            # black + isort + flake8 + mypy
./bin/lint.sh --fix      # auto-format
pytest
git config core.hooksPath .githooks   # enable the staged-file lint hook
```

## ⚠️ Security / trust

lou-op runs **agent-written code and your `success_criteria` commands as
subprocesses** in the job's working directory. There is no sandbox in the MVP —
run only task lists and backends you trust. Sandboxing (Docker/Modal isolation)
is on the roadmap.
