# lou-op POC Runbook

Goal: prove a self-hosted / privately-served near-frontier model can build
real code through lou-op's loop with Claude-tier reliability. Three provider
trials: OpenRouter (fastest to start), Baseten, Modal.

## 0. Pre-flight — always ping first

One round-trip; verifies auth AND that the model emits `tool_calls`
(the native backend is dead without them):

```bash
source .venv/bin/activate
lou-op ping                      # uses env below
```

✓ = go. "NO tool_calls" = wrong model or missing vLLM flags
(`--enable-auto-tool-choice --tool-call-parser <model>`). 401 with
Baseten = set `LOU_AUTH_SCHEME=Api-Key`.

## 1. Provider configs

### OpenRouter
```bash
export OPENROUTER_API_KEY=sk-or-...
export LOU_OPENROUTER_BASE_URL=https://openrouter.ai/api/v1
export LOU_MODEL_ID=z-ai/glm-4.6          # tool-capable; or qwen3-coder, deepseek
export LOU_BACKEND=native
```

### Baseten
```bash
export OPENROUTER_API_KEY=<baseten-api-key>
export LOU_AUTH_SCHEME=Api-Key             # Baseten, not Bearer
export LOU_OPENROUTER_BASE_URL=https://model-XXXX.api.baseten.co/environments/production/sync/v1
export LOU_MODEL_ID=<deployed-model-name>
export LOU_BACKEND=native
```

### Modal (self-hosted vLLM — the data-privacy path)
```bash
# your vLLM deploy must pass: --enable-auto-tool-choice --tool-call-parser <parser>
export OPENROUTER_API_KEY=<token>
export LOU_OPENROUTER_BASE_URL=https://<you>--<app>.modal.run/v1
export LOU_MODEL_ID=<served-model>
export LOU_BACKEND=native
# optional: run validators + model bash inside a Modal Sandbox too
#   pip install modal && modal token new
#   add --runtime modal to the run command
```

## 2. Safety rails (all default-on)

| Rail | Default | Override |
|---|---|---|
| Sandbox egress | **deny** | `--sandbox-network` / `LOU_SANDBOX_NETWORK=on` |
| Subprocess env | strict allowlist | task `passthrough` (code-level) |
| Validators required | yes | `allow_no_validators: true` per task |
| Job wall clock | 2h (`timeout_seconds`) | per JobSpec |
| Token cap | unlimited | `LOU_MAX_JOB_TOKENS=200000` |
| Scope | declared `allowed_paths` | `--strict-scope` infers when empty |
| Runtime | host | `--runtime docker` / `--runtime modal` |

## 3. Run

```bash
lou-op run tasks.yaml --backend native --runtime docker --strict-scope
```

Watch for:
- `[guard] validators pass before any work` → your spec is vacuous, fix it
- `[guard] reverting out-of-scope change` → model wandered; scope caught it
- `[native] token budget exhausted` → raise cap or shrink task
- audit trail: `.lou-op/audit.jsonl` (every tool call + tokens per iteration)

## 4. Qualify a model before trusting it

```bash
lou-op bench tasks.yaml --backend native --runs 3
# task-name / pass_rate / mean_iterations  (iteration 0 preflight excluded)
```

Pass rate < 2/3 on your demo task → model too weak for the loop; try a
bigger one before blaming the harness.

## 5. Demo task (spec/impl split)

`tasks.demo.yaml` + `tests/demo/test_slug.py` — seed spec is protected,
impl scoped to one file. Green run proves: red preflight → model iterates
→ guards hold → validators gate → audit written.
