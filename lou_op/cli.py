"""Local CLI runner: ``python -m lou_op.cli run tasks.yaml``."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from .config import Settings
from .models import JobSpec
from .orchestrator import JobManager, load_tasks


def _run(args: argparse.Namespace) -> int:
    tasks_path = Path(args.tasks)
    if not tasks_path.exists():
        print(f"tasks file not found: {tasks_path}", file=sys.stderr)
        return 2

    tasks = load_tasks(tasks_path)
    settings = Settings.from_env()
    if args.jobs_dir:
        settings.jobs_dir = Path(args.jobs_dir)

    spec = JobSpec(
        project_name=args.project_name or tasks_path.stem,
        tasks=tasks,
        backend=args.backend or settings.default_backend,
        git_remote=args.remote,
    )

    manager = JobManager(settings)
    state = manager.create(spec, run_async=False, tasks_path=tasks_path)

    print(f"job {state.job_id} [{state.status.value}] branch {state.git_branch}")
    repo = settings.jobs_dir / state.job_id
    print(f"repo: {repo}")
    for line in state.commits:
        print(f"  {line}")
    if state.error:
        print(f"error: {state.error}", file=sys.stderr)
        return 1
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="lou-op")
    sub = parser.add_subparsers(dest="command", required=True)

    run = sub.add_parser("run", help="run a tasks.yaml to completion")
    run.add_argument("tasks", help="path to tasks.yaml")
    run.add_argument("--backend", help="mock | agent-cli | raw-api")
    run.add_argument("--project-name", dest="project_name", default="")
    run.add_argument("--remote", default=None, help="git remote URL to push to")
    run.add_argument("--jobs-dir", dest="jobs_dir", default="")
    run.set_defaults(func=_run)

    args = parser.parse_args(argv)
    return int(args.func(args))


if __name__ == "__main__":
    raise SystemExit(main())
