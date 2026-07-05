# PRD: lou-op root-and-stem refactor — one domain, three ports, one working tree

> Supersedes the incremental PRDs ([v1](PRD.md)/[v2](PRD_v2.md)/[v3](PRD_v3.md)) as the **target architecture**. The POC proved the idea; this rebuilds the workflow to meet the quality bar. Nothing is sacred.
>
> Incorporates a second architect pass, folded inline: a hard-kill/preemption lifecycle (**I8**), a text-protocol agent capability mode (keep the fallback, don't delete it), honest delegated-CLI isolation limits, event snapshots + versioning, a first-class `Planner` (the PRD front door), and an integration-first testing strategy.

## Mandate (read first — this inverts the usual LLM bias)

Optimize decisions for, in priority order: **quality, simplicity, robustness, scalability, long-term maintainability.** **Development cost is explicitly NOT a tie-breaker.** Where a cheaper patch and a more correct rebuild conflict, choose the rebuild. This document deliberately picks expensive-but-right options (event sourcing, a workspace rewrite, a state-machine loop, collapsing backends) that a cost-biased process would avoid. The only concession to pragmatism is *sequencing*, so the system stays working between phases — not scope.

## Diagnosis in one line

Today lou-op is a loop that reaches into git, a sandbox, and a model through scattered hooks; **the working tree is an implicit shared global that three layers (loop, runtime, backend) race on.** Every coherence bug we've found (capped by the A1 guard/validate divergence) is a symptom of that single root cause. A1 was patched with a sync hook; the disease remains.

## Target shape

A **pure domain** (loop + gate + task graph) that speaks to the world through **three ports**, with the working tree owned by exactly one abstraction.

```
                 ┌─────────────────────────────────────────┐
 interfaces      │   cli          api          (future ui)  │
                 └───────────────┬─────────────────────────┘
                                 │ calls
                 ┌───────────────▼─────────────────────────┐
   DOMAIN        │  Run (state machine)                     │
 (imports        │  Iteration state machine                 │
  nothing        │  TaskGraph      Verification / Gate       │
  outward)       │  Scope policy   stop policies             │
                 └───┬───────────────┬───────────────┬──────┘
                     │ Workspace      │ Provider       │ Store
                     │ (the tree)     │ (inference)    │ (state)
        ┌────────────▼───┐  ┌─────────▼────────┐  ┌────▼─────────┐
 adapters│ host / docker /│  │ openrouter /     │  │ sqlite / fs  │
        │ modal / e2b    │  │ baseten / local  │  │ (event log)  │
        └────────────────┘  └──────────────────┘  └──────────────┘
```

- **Domain** depends on nothing but the three port *interfaces*.
- **Adapters** implement ports. Swappable without touching the domain.
- **Interfaces** (cli/api) drive the domain; they are thin.

## The three ports

1. **`Workspace` — owns the working tree, and is the ONLY way anything reads, writes, or executes.**
   `read/write/list/delete`, `exec(cmd, deadline?) -> Result` **with hard-cancel semantics — kill the process group / container, not a cooperative flag**, `changed_paths()`, `snapshot()/restore()`, `commit()/log()/diff()`. Bound to exactly one locus (host dir, docker `/work`, modal sandbox). Guards, validators, the agent, and commits *all* go through it. "Sandboxed" is a property of the Workspace, not a bolt-on. **The A1 bug class is impossible here — there is only ever one tree.**

2. **`Provider` — all model inference, with usage/cost/retry built in.**
   `complete(messages, tools?) -> Completion` where `Completion` always carries token usage + cost. Streaming + retry policy live here. One implementation per vendor (OpenRouter/Baseten/local OpenAI-compatible). The judge and every agent use this single path. **Cost caps and token observability fall out by construction.**

3. **`Store` — durable, event-sourced run state.**
   `append(event)`, `load(run_id) -> Run`, `subscribe(run_id)`. An append-only event log is the single source of truth. Events are **versioned** (a schema tag per event) and the fold is checkpointed by **periodic state snapshots**, so replay of a long run is bounded, not O(history). git commits, `progress.md`, and the audit trail become **projections/artifacts** of the log, not competing stores. Adapters: sqlite (default), fs.

## The domain model

- **`Run`** — a job: an immutable config snapshot + a `TaskGraph`, its state *derived* from the event log. A state machine: `CREATED → RUNNING → (COMPLETED | FAILED | TIMED_OUT | CANCELLED)`.
- **`TaskGraph`** — pure: `ready() -> [Task]`, `is_complete()`, cycle/failed-dep detection. No I/O.
- **`Task`** — `id`, `intent`, `Verification`, `Scope`, status derived from events.
- **`Iteration`** — a pure state machine: `GENERATE → GUARD → VALIDATE → COMMIT → decide(DONE | CONTINUE | STOP)`. Given inputs (agent output, guard result, verdict) it returns the next state. No side effects — mechanisms happen in ports, sequenced by the machine. The machine is **preemptible**: a deadline or cancel forces an `INTERRUPT` transition that hard-cancels the in-flight `exec` and lands the run in `TIMED_OUT`/`CANCELLED` — termination is never deferred to the next boundary (I8).
- **`Verification` (the gate)** — first-class: typed `criteria` (test/lint/coverage/custom), `provenance` (who authored the spec: implementer vs independent), `frozen: bool`, and `evaluate(workspace) -> Verdict`. The integrity axis is the center of the model. A `Verification` is **invalid unless it can fail**: the anti-vacuous check (a gate that passes against an empty/absent implementation is rejected before iteration 1) is a typed precondition of the gate, not a runtime guard — otherwise a spec can be frozen-but-useless.
- **`Scope`** — allowed/protected globs as a policy object: `enforce(workspace) -> reverts`. Fails **closed**.
- **`Planner`** — the PRD front door as a domain concern, not a preprocessing script: `plan(prd) -> (TaskGraph, [Verification])`. It authors the specs and freezes them, and its output carries provenance (I4). This is the product's primary entry point, so its integrity surface (spec authorship, freezing, anti-vacuous) is modeled here, not bolted on.

## Global invariants (the acceptance spine — must always hold)

- **I1 — one tree.** Exactly one Workspace per task-run; nothing touches files or runs commands except through it.
- **I2 — gate coherence.** Verification evaluates against the *same* Workspace the agent mutated and the guards operated on. (A1 by construction.)
- **I3 — state is the log.** `Run` state is derivable solely from the Store's event log; process memory is a cache. Kill the process at any point → resume exactly.
- **I4 — verifier independence.** The implementer never authors or grades its own gate; `Verification` carries provenance and a frozen lifecycle.
- **I5 — accounted inference.** All model calls go through `Provider`; usage/cost is always recorded and cap-enforceable.
- **I6 — dependency direction.** The domain imports nothing from adapters or interfaces.
- **I7 — no bypass.** Every model-influenced execution (native tools, delegated CLIs, validators, guards) runs in the task's Workspace locus. No backend touches the host directly.
- **I8 — bounded termination.** A run halts within a bounded interval of its deadline or a cancel, enforced by real preemption (process-group / container kill), never by cooperative polling at iteration boundaries. A runaway `exec` cannot outlive its deadline.

---

# Phases (staged so the system stays working; scope is not negotiable, only order)

### Phase 1 — Workspace keystone (do first; removes the most risk)

**Problem.** Three layers each assume where the tree is; sync hooks paper over it.
**Target.** Introduce the `Workspace` port + host/docker/modal adapters. Route **all** file access, shell, guards, validators, and commits through it. Delete `sync_in/sync_out` as a public concept — a no-shared-FS adapter (modal) handles transfer *internally* and opaquely; the domain only ever sees one tree.
**Enforces.** I1, I2, I7.
**Acceptance.** `test_guards_visible_to_sandbox_validators` passes for *every* Workspace adapter, unchanged, with no loop-level sync calls. A backend cannot write outside its Workspace (no host path reachable). Removing the docker adapter and adding modal changes zero domain code.

### Phase 2 — Loop as a pure state machine

**Problem.** `run_task` is a 300-line god-function mixing policy, mechanism, orchestration.
**Target.** An `Iteration`/`Run` state machine with explicit states + transitions; stop-conditions, gate evaluation, and scope as injected **policies**; git/fs/exec/inference behind ports. The machine is pure and unit-testable with fakes.
**Enforces.** simplicity, quality, testability.
**Acceptance.** The full loop is exercised with in-memory fakes for all ports — no sandbox, no git, no model. Each transition has a focused test. `run_task` no longer exists as a monolith.

### Phase 3 — Event-sourced durable state (`Store`)

**Problem.** In-memory `JobManager` dicts (lost on restart), partial `metadata.json`, competing state stores.
**Target.** Every state change is an event appended to the `Store`. `Run` is rebuilt by folding events. `progress.md`, git, audit become projections. Resume, parallel bookkeeping, and observability derive from one log. Events are **versioned** and the fold is checkpointed by **periodic snapshots** so replay of a long run is bounded, not O(history).
**Enforces.** I3, robustness, scalability.
**Acceptance.** Kill the process mid-run at any point; restart reconstructs exact state from the Store and resumes with no duplicated work. A long run's resume cost is bounded by the latest snapshot, not full history. An older event schema still folds after an event-shape change. `/status` and audit are pure reads of the log.

### Phase 4 — Unified `Provider` port

**Problem.** 3–4 ad-hoc HTTP clients (native/raw-api/judge/extractor), no shared usage/cost/retry.
**Target.** One `Provider` interface; per-vendor adapters; usage+cost on every `Completion`; retries + timeouts centralized. Judge and all agents use it.
**Enforces.** I5, maintainability.
**Acceptance.** Per-iteration and per-run token/cost appear in the event log and `/status`. A per-run `max_cost_usd` aborts cleanly and deterministically (the C1 cap, now structural). Adding Baseten is a config value, not code.

### Phase 5 — Collapse backends into one uniform agent contract

**Problem.** Four backends with incompatible trust/capability semantics; agent-cli bypasses isolation; raw-api is a strictly-worse native **only when the endpoint emits reliable tool-calls**.
**Target.** One `Agent` contract: given an `Iteration`, a `Workspace`, and a `Provider`, it mutates the workspace. The contract carries an explicit **capability mode**: native `tool_calls` where the endpoint supports them, and a **text file-protocol fallback** where it does not — the common case for open-weight models on vLLM without `--enable-auto-tool-choice`, i.e. exactly the models this product targets. Fold today's raw-api into that fallback mode rather than keeping it as a separate backend; **do not lose the text path**. Make delegated CLIs (claude/codex) run *inside* the Workspace. Capabilities declared explicitly; all agents honor I1/I5/I7.
**Enforces.** I7, robustness, simplicity.
**Acceptance.** Every agent runs inside its Workspace and is accounted identically. A native agent's only egress is its `Provider`; a **delegated CLI is explicitly second-class for isolation** — it must reach a third-party vendor, so `--runtime modal` contains its filesystem and exec but *cannot* close its vendor egress (which is the very leak the privacy thesis exists to prevent). This limit is documented, not papered over. One `Agent` contract with two capability modes, not four backends.

### Phase 6 — First-class `Verification` / gate model

**Problem.** The gate is shell strings + a `lint` bool + a fail-open, injectable judge — the product's soul, under-modeled.
**Target.** `Verification` with typed criteria, `Verdict`, `provenance`, and a **frozen-spec lifecycle**. Judge becomes a *typed advisory signal* (never a silent gate; fenced inputs; fail-closed when declared as gating). Verifier-independence (I4) is a structural precondition, not a checklist item.
**Enforces.** I4, quality.
**Acceptance.** A gate authored by the implementer cannot be marked as the authoritative verifier. Freezing a spec makes it immutable to the agent by construction. A `Verification` that passes against an empty implementation is rejected at plan time, so a frozen spec can never be frozen-but-vacuous. Judge output can never flip a run to "passed" on its own.

### Phase 7 — Layering, config, and the concurrency model

**Problem.** Flat 28-module tangle, module-level singletons, deep `os.environ` reads, thread-mutated shared `Task` objects, unbounded queues.
**Target.** Package by layer (`domain/`, `ports/`, `adapters/`, `interfaces/`) with enforced dependency direction. One typed config built at the edge and injected — no singletons. Concurrency becomes **isolated units of work**: each task-run owns its Workspace (worktree/sandbox), consumes immutable inputs, emits events; the scheduler is a pure function over the graph + Store. Each unit is **preemptible with real kill semantics** (process-group or container), so a deadline or cancel stops it promptly regardless of what it is doing (I8) — not at the next iteration boundary. Same model scales from one process to many.
**Enforces.** I3, I6, I8, scalability, maintainability.
**Acceptance.** An import-linter test enforces I6 (domain imports nothing outward). No module-level mutable singletons. Parallel task-runs share no mutable memory — only the Store. The scheduler is unit-tested as a pure function. A task deliberately spinning past its deadline is killed within a bounded interval, not at the next iteration boundary.

---

## Non-goals

- Preserving the current module layout, the four-backend split, or `tasks.yml` as the primary input. **The PRD-first front door is the primary interface, not an add-on** — it is modeled as the domain `Planner` (see domain model), and `tasks.yml` becomes a secondary, hand-authored path into the same `TaskGraph`.
- A GUI; multi-tenant SaaS ops (durable state makes them *possible*, but they're out of scope here).
- Backward compatibility as a constraint on the design. Provide a migration path, but do not let it shape the architecture.

## Testing strategy (integration-first, deterministic)

The bugs this refactor exists to kill — A1, the twin path-jails, the prefix collision — were all **composition** bugs: every unit was green in isolation while the assembled behavior was wrong. Mock-heavy unit tests test the mocks. So the suite is weighted deliberately, and the ports are part of what makes that cheap.

- **Primary — run-level integration tests.** Drive a whole `Run` through the real domain + a real temp-git `Workspace` + a **deterministic fake `Provider`** (a scripted model). Invariants I1–I8 are asserted here as *outcomes* ("an out-of-scope write never lands", "the gate sees the guard-restored tree", "no egress by default", "a runaway is killed within bound"). This is the bulk of the suite: fast, deterministic, real code paths. Making these tests cheap is a reason the `Provider`/`Workspace` ports exist, not a side effect.
- **Security tier — targeted adversarial tests at the `Workspace` only.** Jail escapes (prefix, symlink, dangling), filenames with spaces, scope denial, egress default-deny. The one place focused low-level tests remain, because you must enumerate attack inputs and want a failure to localize to the exact guard.
- **Contract tests per adapter.** One shared suite run against every `Workspace` (host/docker/modal), every `Provider` (openrouter/baseten/local), and every `Store` (sqlite/fs), proving substitutability (the "swap changes zero domain code" DoD). Live adapters (real modal, real vendor) sit behind a skip marker.
- **A few true-e2e smoke tests** through cli/api with the fake `Provider` — wiring only.
- **The real model is never a CI gate.** It is a manual qualification/bench run. The flaky open-weight model is the object under test, not part of the merge signal.

**Rule:** I1–I8 are enforced at **integration level by default**, and at unit level **only in the security tier**. A satisfied-by-mocks unit test does not count as enforcing an invariant — that is how false green returns.

## Definition of done

- All eight invariants (I1–I8) hold and are enforced by tests (import-linter for I6; run-level integration/property tests for the rest, per the testing strategy above).
- The domain runs end-to-end against **in-memory fakes** for all three ports — no model, no sandbox, no git required to test the core.
- Swapping any adapter (host↔docker↔modal; openrouter↔baseten; sqlite↔fs) changes **zero** domain code.
- A run survives process death at any point and resumes exactly from the Store, with resume cost bounded by the latest snapshot.
- A run halts within a bounded interval of its deadline or cancel (I8), enforced by real kill, not boundary polling.
- The A1-class of bugs is structurally unreachable (I1/I2), and cost/usage is always accounted (I5).

## Sequencing rationale

Phase 1 (Workspace) is the keystone: it dissolves the largest bug class and is the precondition for a clean loop. Phases 2–4 give the domain its shape (pure loop, durable state, accounted inference). Phase 5 unifies the agents onto that shape. Phase 6 elevates the gate — the differentiator — to a first-class citizen. Phase 7 locks in the layering and the scale model. Each phase leaves the system runnable; none is optional.
