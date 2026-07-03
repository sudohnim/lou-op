"""Job orchestration: load tasks, run them sequentially, track + persist state.

Task status is written back to ``tasks.yaml`` after each task so a crashed or
timed-out job resumes at the first unfinished task. Job metadata is persisted
to ``.lou-op/metadata.json`` in the work repo.
"""

from __future__ import annotations

import queue
import threading
import uuid
from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, wait
from pathlib import Path
from typing import Callable, Dict, List, Optional

import yaml

from .backends.extractor import LLMClient
from .backends.raw_api import OpenRouterClient
from .backends.registry import get_backend
from .config import Settings
from .git_ops import log
from .judge import ConsistencyJudge
from .loop import run_task
from .models import JobSpec, JobState, JobStatus, Task, TaskStatus
from .runtime import get_runtime
from .validators import build_validators
from .workspace import GitWorkspace, NullWorkspace, Workspace


class DependencyError(Exception):
    """Raised when task dependencies cannot be satisfied."""

    pass


def validate_tasks(tasks: List[Task]) -> None:
    """Reject unverifiable tasks: no criteria, no lint, no judge — nothing
    would gate 'passed'. Opt out per-task with ``allow_no_validators``."""
    for task in tasks:
        if (
            not task.success_criteria
            and not task.lint
            and not task.judge
            and not task.allow_no_validators
        ):
            raise ValueError(
                f"task '{task.name}' has no validators (no success_criteria,"
                " lint: false, judge: false) — its result would be"
                " meaningless. Add one or set allow_no_validators: true."
            )


def load_tasks(path: Path) -> List[Task]:
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    raw = data.get("tasks", [])
    tasks = [Task.model_validate(item) for item in raw]
    validate_tasks(tasks)
    return tasks


def write_tasks(path: Path, tasks: List[Task]) -> None:
    payload = {"tasks": [t.model_dump(mode="json") for t in tasks]}
    path.write_text(yaml.safe_dump(payload, sort_keys=False), encoding="utf-8")


def _make_judge_client(settings: Settings) -> LLMClient | None:
    if not settings.openrouter_api_key:
        return None
    return OpenRouterClient(
        api_key=settings.openrouter_api_key,
        model_id=settings.model_id,
        timeout=settings.inference_timeout_s,
    )


def _make_consistency_judge(task: Task, settings: Settings) -> ConsistencyJudge | None:
    """Return a ConsistencyJudge if task.judge is enabled, else None."""
    if not task.judge:
        return None
    client = _make_judge_client(settings)
    if client is None:
        return None
    return ConsistencyJudge(client)


def _make_workspace(spec: JobSpec, settings: Settings) -> Workspace:
    project_path = Path(spec.project_path) if spec.project_path else None
    key = spec.workspace_type.strip().lower()
    if key == "git":
        return GitWorkspace(
            settings.jobs_dir, remote=spec.git_remote, project_path=project_path
        )
    if key == "null":
        return NullWorkspace(settings.jobs_dir)
    raise ValueError(f"unknown workspace_type: {spec.workspace_type!r}")


def ready_tasks(tasks: List[Task]) -> List[Task]:
    """All tasks runnable right now: PENDING/IN_PROGRESS with every
    dependency PASSED, in list order. (Cycle/failed-dep detection lives in
    select_next_task / run_parallel — this is the pure readiness filter.)"""
    by_name = {t.name: t for t in tasks}
    out: List[Task] = []
    for task in tasks:
        if task.status not in (TaskStatus.PENDING, TaskStatus.IN_PROGRESS):
            continue
        deps = [by_name.get(name) for name in task.depends_on]
        if any(d is None for d in deps):
            continue
        if all(d.status == TaskStatus.PASSED for d in deps):
            out.append(task)
    return out


def run_parallel(
    tasks: List[Task],
    run_one: Callable[[Task], bool],
    *,
    max_parallel: int = 1,
    on_status: Optional[Callable[[Task], None]] = None,
) -> None:
    """Run the task DAG, up to ``max_parallel`` concurrently.

    ``run_one(task) -> passed`` does the actual work. Dependents start only
    after all dependencies PASS. Fail-fast: after any failure no new tasks
    start (in-flight ones drain). Raises DependencyError if tasks remain
    PENDING when nothing more can run (failed/unknown dep or cycle).
    max_parallel=1 reproduces the serial order exactly.
    """
    bound = max(1, max_parallel)
    lock = threading.Lock()
    failed = False

    with ThreadPoolExecutor(max_workers=bound) as pool:
        futures: Dict[Future, Task] = {}

        def submit_ready() -> None:
            # caller holds `lock`
            if failed:
                return
            for task in ready_tasks(tasks):
                if len(futures) >= bound:
                    break
                if task.status == TaskStatus.IN_PROGRESS:
                    continue  # already submitted
                task.status = TaskStatus.IN_PROGRESS
                if on_status:
                    on_status(task)
                futures[pool.submit(run_one, task)] = task

        with lock:
            submit_ready()
        while futures:
            done, _ = wait(list(futures), return_when=FIRST_COMPLETED)
            for future in done:
                task = futures.pop(future)
                passed = future.result()  # propagate run_one exceptions
                with lock:
                    task.status = TaskStatus.PASSED if passed else TaskStatus.FAILED
                    if on_status:
                        on_status(task)
                    if not passed:
                        failed = True
                    submit_ready()

    remaining = [t for t in tasks if t.status == TaskStatus.PENDING]
    if remaining:
        names = ", ".join(t.name for t in remaining)
        raise DependencyError(
            f"tasks blocked and cannot run: {names}"
            " (failed dependency, unknown dependency, or cycle)"
        )


def select_next_task(tasks: List[Task]) -> Optional[Task]:
    """Return the next runnable task, dependency-aware and crash-safe.

    IN_PROGRESS tasks (interrupted by a crash) are resumed first, then
    PENDING tasks — both only when their dependencies are all PASSED.

    Raises DependencyError if:
    - A dependency is FAILED
    - A dependency name does not exist
    - A dependency cycle is detected

    Returns None when no task is IN_PROGRESS or PENDING.
    """
    task_by_name = {t.name: t for t in tasks}
    # resume interrupted work before starting fresh work
    pending_tasks = [t for t in tasks if t.status == TaskStatus.IN_PROGRESS] + [
        t for t in tasks if t.status == TaskStatus.PENDING
    ]

    if not pending_tasks:
        return None

    def has_cycle(task_name: str, visited: set[str], rec_stack: set[str]) -> bool:
        """Check if task_name is part of a dependency cycle."""
        if task_name in rec_stack:
            return True
        if task_name in visited:
            return False

        visited.add(task_name)
        rec_stack.add(task_name)

        task = task_by_name.get(task_name)
        if task:
            for dep_name in task.depends_on:
                if has_cycle(dep_name, visited, rec_stack):
                    return True

        rec_stack.remove(task_name)
        return False

    def are_deps_satisfied(task: Task) -> bool:
        """Check if all dependencies of task are satisfied (PASSED)."""
        for dep_name in task.depends_on:
            if dep_name not in task_by_name:
                raise DependencyError(
                    f"task '{task.name}' depends on unknown task '{dep_name}'"
                )
            dep_task = task_by_name[dep_name]
            if dep_task.status == TaskStatus.FAILED:
                raise DependencyError(
                    f"task '{task.name}' depends on failed task '{dep_name}'"
                )
            if dep_task.status != TaskStatus.PASSED:
                return False
        return True

    for task in pending_tasks:
        try:
            if are_deps_satisfied(task):
                return task
        except DependencyError:
            raise

    for task in pending_tasks:
        if has_cycle(task.name, set(), set()):
            raise DependencyError(f"dependency cycle detected involving '{task.name}'")

    raise DependencyError(
        "pending tasks exist but none can run "
        "(check for failed dependencies or cycles)"
    )


class JobManager:
    """In-memory job registry that runs jobs on background threads."""

    def __init__(self, settings: Optional[Settings] = None) -> None:
        self.settings = settings or Settings.from_env()
        self._jobs: Dict[str, JobState] = {}
        self._threads: Dict[str, threading.Thread] = {}
        self._log_queues: Dict[str, "queue.Queue[Optional[str]]"] = {}

    # -- public API ---------------------------------------------------------

    def create(
        self,
        spec: JobSpec,
        *,
        run_async: bool = True,
        tasks_path: Optional[Path] = None,
    ) -> JobState:
        validate_tasks(spec.tasks)  # API path gets the same gate as yaml load
        job_id = uuid.uuid4().hex[:12]
        branch = f"lou-op/job-{job_id}"
        state = JobState(
            job_id=job_id,
            status=JobStatus.PENDING,
            git_branch=branch,
            project_name=spec.project_name,
        )
        self._jobs[job_id] = state
        self._log_queues[job_id] = queue.Queue()
        if run_async:
            thread = threading.Thread(
                target=self._run,
                args=(state, spec, tasks_path),
                daemon=True,
            )
            self._threads[job_id] = thread
            thread.start()
        else:
            self._run(state, spec, tasks_path)
        return state

    def get_status(self, job_id: str) -> Optional[JobState]:
        return self._jobs.get(job_id)

    def get_results(self, job_id: str) -> Optional[JobState]:
        return self._jobs.get(job_id)

    def get_log_queue(self, job_id: str) -> Optional["queue.Queue[Optional[str]]"]:
        return self._log_queues.get(job_id)

    # -- internals ----------------------------------------------------------

    def _repo_path(self, job_id: str) -> Path:
        return self.settings.jobs_dir / job_id

    def _run(self, state: JobState, spec: JobSpec, tasks_path: Optional[Path]) -> None:
        try:
            self._execute(state, spec, tasks_path)
        except Exception as exc:  # noqa: BLE001 - surface any failure to status
            state.status = JobStatus.FAILED
            state.error = str(exc)
        finally:
            log_q = self._log_queues.get(state.job_id)
            if log_q is not None:
                log_q.put(None)  # signal SSE consumer that job is done
        self._write_metadata(state)

    def _write_metadata(self, state: JobState) -> None:
        repo_path = self._repo_path(state.job_id)
        meta_dir = repo_path / ".lou-op"
        if not meta_dir.exists():
            return
        (meta_dir / "metadata.json").write_text(
            state.model_dump_json(indent=2), encoding="utf-8"
        )

    def _execute(
        self, state: JobState, spec: JobSpec, tasks_path: Optional[Path]
    ) -> None:
        state.status = JobStatus.RUNNING
        workspace = _make_workspace(spec, self.settings)
        workspace.setup(state.job_id, state.git_branch)

        log_q = self._log_queues.get(state.job_id)

        def on_line(line: str) -> None:
            if log_q is not None:
                log_q.put(line.rstrip("\n"))

        backend = get_backend(spec.backend, self.settings)
        tasks = list(spec.tasks)

        # sandbox: model-influenced commands (success_criteria) run in the
        # selected runtime; "host" (default) is the pre-runtime behavior.
        runtime = get_runtime(
            spec.runtime or self.settings.runtime,
            network=self.settings.sandbox_network,
        )
        max_parallel = max(1, self.settings.max_parallel)
        if max_parallel > 1 and spec.workspace_type != "null":
            # one git working tree cannot host concurrent index operations;
            # per-task clones/worktrees are the v-next unlock
            raise ValueError(
                "--max-parallel > 1 currently requires workspace_type: null"
                " (git workspaces share one working tree)"
            )

        state_lock = threading.Lock()

        def run_one(task: Task) -> bool:
            with state_lock:
                state.current_task = task.name
            validators = build_validators(
                task,
                self.settings.inference_timeout_s,
                shell_fn=runtime.shell,
            )
            consistency_judge = _make_consistency_judge(task, self.settings)
            results = run_task(
                workspace.path,
                task,
                backend,
                workspace=workspace,
                validators=validators,
                consistency_judge=consistency_judge,
                budget=self.settings.context_budget_tokens,
                timeout=self.settings.inference_timeout_s,
                strict_scope=self.settings.strict_scope,
                on_line=on_line,
            )
            passed = bool(results) and results[-1].done
            with state_lock:
                state.commits.extend(r.commit for r in results)
                if passed:
                    state.completed_tasks.append(task.name)
            return passed

        def on_status(task: Task) -> None:
            # status writeback after every transition — crash-safe resume
            if tasks_path is not None:
                write_tasks(tasks_path, tasks)

        runtime.setup(state.job_id, workspace.path)
        try:
            run_parallel(
                tasks,
                run_one,
                max_parallel=max_parallel,
                on_status=on_status,
            )
        except DependencyError:
            failed = [t.name for t in tasks if t.status == TaskStatus.FAILED]
            state.status = JobStatus.FAILED
            state.error = (
                f"task '{failed[0]}' did not pass"
                if failed
                else "tasks blocked by dependencies"
            )
            return
        finally:
            runtime.teardown()

        if any(t.status == TaskStatus.FAILED for t in tasks):
            failed = [t.name for t in tasks if t.status == TaskStatus.FAILED]
            state.status = JobStatus.FAILED
            state.error = f"task '{failed[0]}' did not pass"
            return

        state.current_task = None
        workspace.teardown(push_remote=bool(spec.git_remote))
        state.commits = log(workspace.path, count=len(state.commits) + 5)
        state.status = JobStatus.COMPLETED
