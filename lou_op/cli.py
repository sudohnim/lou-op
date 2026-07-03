"""Local CLI runner: ``python -m lou_op.cli run tasks.yaml``."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from .backends.registry import get_backend
from .bench import run_bench
from .config import Settings
from .models import JobSpec, Task, TaskStatus
from .orchestrator import JobManager, load_tasks, write_tasks

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


def _run(args: argparse.Namespace) -> int:
    tasks_path = Path(args.tasks)
    if not tasks_path.exists():
        print(f"tasks file not found: {tasks_path}", file=sys.stderr)
        return 2

    tasks = load_tasks(tasks_path)
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

    project_path = tasks_path.parent.resolve()

    spec = JobSpec(
        project_name=args.project_name or tasks_path.stem,
        tasks=tasks,
        backend=args.backend or settings.default_backend,
        git_remote=args.remote,
        project_path=str(project_path),
    )

    manager = JobManager(settings)
    state = manager.create(spec, run_async=True, tasks_path=tasks_path)

    # drain log queue — prints lines in real time, blocks until job ends
    log_q = manager.get_log_queue(state.job_id)
    if log_q is not None:
        while True:
            line = log_q.get()
            if line is None:
                break
            print(line, flush=True)

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

    tasks = load_tasks(tasks_path)
    settings = Settings.from_env()

    project_path = tasks_path.parent.resolve()
    backend = get_backend(args.backend or settings.default_backend, settings)

    report = run_bench(project_path, tasks, backend, runs=args.runs, settings=settings)

    for stats in report.task_stats:
        print(f"{stats.name} / {stats.pass_rate} / {stats.mean_iterations}")

    return 0


def _ping(args: argparse.Namespace) -> int:
    from .backends.native_agent import NativeAgentBackend
    from .ping import ping

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


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="lou-op")
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

    args = parser.parse_args(argv)
    return int(args.func(args))


if __name__ == "__main__":
    raise SystemExit(main())
