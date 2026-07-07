"""Local CLI runner: ``python -m lou_op.cli run tasks.yaml``."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .backends.registry import get_backend
from .bench import run_bench
from .config import Settings, load_project_env
from .logutil import configure_logging, get_logger
from .models import JobSpec, Task, TaskStatus
from .orchestrator import JobManager, load_tasks, write_tasks

log = get_logger()

_DECOMPOSE_PROMPT = """\
You are a project planner for an automated code-generation loop.

Decompose the following task into 2-4 independent sub-tasks. Each sub-task should be:
- Small enough to implement correctly in 1-3 iterations
- Have a clear, verifiable success criterion (a shell command that exits 0 on success)
- Sequenced so earlier tasks don't depend on later ones

Task to decompose:
Name: {name}
Description:
{description}
Success criteria:
{criteria}

Return ONLY valid YAML in this exact format (no other text, no markdown fences):
tasks:
  - name: "Sub-task 1 name"
    description: |
      What to implement.
    success_criteria:
      - "shell command to verify"
    max_iterations: 3
  - name: "Sub-task 2 name"
    description: |
      What to implement.
    success_criteria:
      - "shell command to verify"
    max_iterations: 3
"""


def _decompose_task(task: Task, client: object) -> list[Task]:
    import yaml

    criteria = "\n".join(f"  - {c}" for c in task.success_criteria)
    prompt = _DECOMPOSE_PROMPT.format(
        name=task.name,
        description=task.description.strip(),
        criteria=criteria,
    )
    try:
        response = client.generate(prompt).strip()  # type: ignore[attr-defined]
        # strip markdown fences if model wraps output anyway
        if response.startswith("```"):
            lines = response.splitlines()
            start = 1
            end = len(lines) - 1 if lines[-1].strip() == "```" else len(lines)
            response = "\n".join(lines[start:end])
        data = yaml.safe_load(response)
        raw = data.get("tasks", [])
        return [Task.model_validate(t) for t in raw]
    except Exception as exc:  # noqa: BLE001
        print(
            f"  warning: decomposition failed ({exc}), keeping original",
            file=sys.stderr,
        )
        return []


def _plan(args: argparse.Namespace) -> int:
    tasks_path = Path(args.tasks_file)
    if not tasks_path.exists():
        print(f"tasks file not found: {tasks_path}", file=sys.stderr)
        return 2

    load_project_env(tasks_path.parent.resolve())
    tasks = load_tasks(tasks_path)
    settings = Settings.from_env()

    if not settings.openrouter_api_key:
        print("error: OPENROUTER_API_KEY required for plan command", file=sys.stderr)
        return 1

    from .backends.raw_api import OpenRouterClient

    client = OpenRouterClient(
        api_key=settings.openrouter_api_key,
        model_id=settings.model_id,
        timeout=settings.inference_timeout_s,
    )

    new_tasks: list[Task] = []
    for task in tasks:
        print(f"decomposing: {task.name}")
        decomposed = _decompose_task(task, client)
        if decomposed:
            new_tasks.extend(decomposed)
            print(f"  → {len(decomposed)} sub-tasks")
        else:
            new_tasks.append(task)
            print(f"  → kept as-is (decomposition failed)")

    write_tasks(tasks_path, new_tasks)
    print(f"\nwrote {len(new_tasks)} tasks to {tasks_path}")
    return 0


def _reset(args: argparse.Namespace) -> int:
    tasks_path = Path(args.tasks_file)
    if not tasks_path.exists():
        print(f"tasks file not found: {tasks_path}", file=sys.stderr)
        return 2
    tasks = load_tasks(tasks_path)
    for task in tasks:
        task.status = TaskStatus.PENDING
    write_tasks(tasks_path, tasks)
    print(f"reset {len(tasks)} task(s) to pending: {tasks_path}")
    return 0


def _tasks_from_prd(prd_path, project_path, settings, args):
    """Decompose a markdown PRD into a frozen-spec task graph on disk.

    Returns the tasks, or None on error (message already printed). Unless
    --yes is given, pauses so a human can review the generated specs before
    the impl loop runs (verifier-independence checkpoint, B3).

    If a cached task graph exists from a prior decomposition, the cache is
    reused and the spec model is not called again. Delete .lou-op/tasks.json
    to force a fresh decomposition.
    """
    from .backends.raw_api import OpenRouterClient
    from .prd import build_tasks_from_prd, load_cached_tasks

    # ── Fast path: reuse cached task graph (no API call, no cost) ──────
    cached = load_cached_tasks(project_path)
    if cached is not None:
        print(f"[prd] reusing cached specs ({len(cached)} tasks)")
        print(f"[prd] delete .lou-op/tasks.json to force re-decomposition")

        # Still respect the review checkpoint on first use of a fresh cache
        if not getattr(args, "yes", False):
            print(
                "\nReview the generated spec files above. They are the contract the"
                "\nimplementer is graded against. Re-run with --yes to build, or"
                "\nedit the specs first.",
            )
            return None
        return cached

    # ── Slow path: fresh decomposition via the spec model ──────────────
    if not settings.openrouter_api_key:
        print("error: OPENROUTER_API_KEY required to decompose a PRD", file=sys.stderr)
        return None

    client = OpenRouterClient(
        api_key=settings.openrouter_api_key,
        model_id=settings.spec_model or settings.model_id,
        base_url=settings.openrouter_base_url,
        timeout=settings.inference_timeout_s,
        max_tokens=settings.spec_max_tokens,
    )

    print(f"decomposing PRD {prd_path.name} (spec model: {client.model_id}) ...")

    try:
        tasks = build_tasks_from_prd(
            prd_path.read_text(encoding="utf-8"),
            project_path,
            client.generate,
        )
    except Exception as exc:  # noqa: BLE001
        print(f"error: PRD decomposition failed: {exc}", file=sys.stderr)
        return None

    # verifier-independence record: was the spec author a DIFFERENT model
    # than the implementer, and did a human review the specs?
    independent = bool(settings.spec_model) and (
        settings.spec_model != (args.backend or settings.default_backend)
    )

    provenance = {
        "spec_model": client.model_id,
        "implementer_model": settings.model_id,
        "independent_verifier": independent,
        "reviewed": not getattr(args, "yes", False),
    }
    prov_path = project_path / ".lou-op" / "spec_provenance.json"
    prov_path.parent.mkdir(parents=True, exist_ok=True)
    prov_path.write_text(json.dumps(provenance, indent=2), encoding="utf-8")

    if not getattr(args, "yes", False):
        print(
            "\nReview the generated spec files above. They are the contract the"
            "\nimplementer is graded against. Re-run with --yes to build, or"
            "\nedit the specs first.",
        )
        return None

    return tasks


def _run(args: argparse.Namespace) -> int:
    tasks_path = Path(args.tasks)
    if not tasks_path.exists():
        print(f"tasks file not found: {tasks_path}", file=sys.stderr)
        return 2

    project_path = tasks_path.parent.resolve()
    # read the .env from the project you're running, not lou-op's own dir
    load_project_env(project_path)

    settings = Settings.from_env()
    if args.jobs_dir:
        settings.jobs_dir = Path(args.jobs_dir)
    if getattr(args, "strict_scope", False):
        settings.strict_scope = True
    if getattr(args, "runtime", ""):
        settings.runtime = args.runtime
    if getattr(args, "max_parallel", 0):
        settings.max_parallel = args.max_parallel
    if getattr(args, "sandbox_network", False):
        settings.sandbox_network = True

    writeback_path = tasks_path
    if tasks_path.suffix.lower() in (".md", ".markdown"):
        tasks = _tasks_from_prd(tasks_path, project_path, settings, args)
        if tasks is None:
            return 1
        writeback_path = project_path / ".lou-op" / "generated_tasks.yaml"
        writeback_path.parent.mkdir(parents=True, exist_ok=True)
        write_tasks(writeback_path, tasks)
    else:
        tasks = load_tasks(tasks_path)

    spec = JobSpec(
        project_name=args.project_name or tasks_path.stem,
        tasks=tasks,
        backend=args.backend or settings.default_backend,
        git_remote=args.remote,
        project_path=str(project_path),
    )

    manager = JobManager(settings)

    # run_async=False blocks here; structlog streams the job's progress live
    # to the console (the per-job queue still feeds the API/SSE consumers)
    log.debug("creating job", phase="cli")
    try:
        state = manager.create(spec, run_async=False, tasks_path=writeback_path)
    except KeyboardInterrupt:
        print("\n[interrupt] stopping...", file=sys.stderr)
        return 130
    except Exception as exc:
        log.error("job failed to start", phase="cli", error=str(exc))
        import traceback

        traceback.print_exc()
        return 1

    print(f"\njob {state.job_id} [{state.status.value}] branch {state.git_branch}")
    print(f"repo: {project_path}")
    for line in state.commits:
        print(f"  {line}")
    if state.error:
        print(f"error: {state.error}", file=sys.stderr)
        return 1
    return 0


def _bench(args: argparse.Namespace) -> int:
    tasks_path = Path(args.tasks)
    if not tasks_path.exists():
        print(f"tasks file not found: {tasks_path}", file=sys.stderr)
        return 2

    project_path = tasks_path.parent.resolve()
    load_project_env(project_path)
    tasks = load_tasks(tasks_path)
    settings = Settings.from_env()

    backend = get_backend(args.backend or settings.default_backend, settings)

    report = run_bench(project_path, tasks, backend, runs=args.runs, settings=settings)

    for stats in report.task_stats:
        print(f"{stats.name} / {stats.pass_rate} / {stats.mean_iterations}")

    return 0


def _ping(args: argparse.Namespace) -> int:
    from .backends.native_agent import NativeAgentBackend
    from .ping import ping

    load_project_env()
    settings = Settings.from_env()
    if args.model:
        settings.model_id = args.model
    if not settings.openrouter_api_key:
        print("no OPENROUTER_API_KEY set", file=sys.stderr)
        return 2

    backend = NativeAgentBackend(
        settings.openrouter_base_url,
        settings.openrouter_api_key,
        settings.model_id,
        auth_scheme=settings.auth_scheme,
        request_timeout_s=settings.inference_timeout_s,
    )
    print(
        f"pinging {settings.openrouter_base_url} "
        f"(model={settings.model_id}, auth={settings.auth_scheme}) ..."
    )
    result = ping(backend)
    print(result.render())
    return 0 if result.ok else 1


def _render_why(run_id: str, events: list) -> None:
    """Render the event log as a per-task debugging report: what stopped each
    task and the actual gate output — the single source of truth, no scrolling
    interleaved stderr."""
    by_task: dict[str, list] = {}
    order: list[str] = []
    header = f"job {run_id}"
    footer = ""
    for e in events:
        d = e.data
        if e.kind == "run_finished":
            footer = f"run {d.get('status', '?')}"
            if d.get("error"):
                footer += f" — {d['error']}"
        task = d.get("task")
        if task is None:
            continue
        if task not in by_task:
            by_task[task] = []
            order.append(task)
        by_task[task].append(e)

    print(header)
    for task in order:
        evs = by_task[task]
        finished = next((e for e in evs if e.kind == "task_finished"), None)
        reason = finished.data.get("stop_reason", "?") if finished else "in-flight"
        ok = finished.data.get("passed", False) if finished else False
        mark = "PASS" if ok else "FAIL"
        print(f"\n[{mark}] {task}  ({reason})")
        for e in evs:
            if e.kind != "iteration":
                continue
            d = e.data
            wrote = "" if d.get("wrote_files", True) else " no-write"
            print(
                f"  iter {d.get('n')}: "
                f"{'pass' if d.get('passed') else 'fail'}{wrote}"
            )
            for v in d.get("validators", []):
                st = v.get("status", "?")
                if st == "pass":
                    continue
                print(f"    {st.upper()} {v.get('name')}")
                out = (v.get("output") or "").strip()
                for line in out.splitlines()[-12:]:
                    print(f"      {line}")
    if footer:
        print(f"\n{footer}")


def _why(args: argparse.Namespace) -> int:
    from .adapters.store_sqlite import SqliteStore

    settings = Settings.from_env()
    if args.jobs_dir:
        settings.jobs_dir = Path(args.jobs_dir)
    store = SqliteStore(settings.jobs_dir / "events.db")

    run_id = args.job
    if not run_id:
        ids = store.run_ids()
        if not ids:
            print("no jobs found", file=sys.stderr)
            return 1
        # job ids are uuids (no chronological order) — pick whichever run has
        # the newest event, since seq is a global autoincrement
        run_id = max(ids, key=lambda rid: store.events(rid)[-1].seq)

    events = store.events(run_id)
    if not events:
        print(f"no events for job {run_id}", file=sys.stderr)
        return 1
    _render_why(run_id, events)
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="lou-op")
    parser.add_argument(
        "--log-level",
        default="",
        help="debug | info | warning | error (default: info, or LOU_LOG_LEVEL)",
    )
    parser.add_argument(
        "--json-logs",
        action="store_true",
        help="emit machine-readable JSON logs instead of console output",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    plan = sub.add_parser("plan", help="decompose tasks.yaml into sub-tasks using LLM")
    plan.add_argument("tasks_file", help="path to tasks YAML")
    plan.set_defaults(func=_plan)

    reset = sub.add_parser("reset", help="reset all tasks in a YAML file to pending")
    reset.add_argument("tasks_file", help="path to tasks YAML")
    reset.set_defaults(func=_reset)

    run = sub.add_parser("run", help="run a tasks.yaml to completion")
    run.add_argument("tasks", help="path to tasks.yaml")
    run.add_argument("--backend", help="mock | agent-cli | native | raw-api")
    run.add_argument("--project-name", dest="project_name", default="")
    run.add_argument("--remote", default=None, help="git remote URL to push to")
    run.add_argument("--jobs-dir", dest="jobs_dir", default="")
    run.add_argument(
        "--strict-scope",
        dest="strict_scope",
        action="store_true",
        help="tasks without allowed_paths get scope inferred from description",
    )
    run.add_argument(
        "--runtime",
        default="",
        help="host | docker (sandbox model-run commands in a container)",
    )
    run.add_argument(
        "--max-parallel",
        dest="max_parallel",
        type=int,
        default=0,
        help="run up to N dependency-satisfied tasks concurrently (default 1)",
    )
    run.add_argument(
        "--sandbox-network",
        dest="sandbox_network",
        action="store_true",
        help="allow network egress inside the sandbox (default: deny)",
    )
    run.add_argument(
        "--yes",
        dest="yes",
        action="store_true",
        help="skip the PRD spec-review checkpoint and build immediately",
    )
    run.set_defaults(func=_run)

    bench = sub.add_parser(
        "bench", help="benchmark tasks: measure pass rate and iteration count"
    )
    bench.add_argument("tasks", help="path to tasks.yaml")
    bench.add_argument("--backend", help="mock | agent-cli | raw-api")
    bench.add_argument(
        "--runs", type=int, default=3, help="number of runs per task (default: 3)"
    )
    bench.set_defaults(func=_bench)

    ping_p = sub.add_parser(
        "ping", help="smoke-test the native endpoint: auth + tool-calling round-trip"
    )
    ping_p.add_argument("--model", default="", help="override LOU_MODEL_ID")
    ping_p.set_defaults(func=_ping)

    why = sub.add_parser(
        "why",
        help="explain a job from the event log: per-task stop reason + gate output",
    )
    why.add_argument("job", nargs="?", default="", help="job id (default: latest)")
    why.add_argument("--jobs-dir", dest="jobs_dir", default="")
    why.set_defaults(func=_why)

    args = parser.parse_args(argv)
    configure_logging(
        json_logs=args.json_logs or None,
        level=args.log_level or None,
    )
    return int(args.func(args))


if __name__ == "__main__":
    raise SystemExit(main())
