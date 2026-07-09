"""Job orchestration: load tasks, run them sequentially, track + persist state.

Task status is written back to ``tasks.yaml`` after each task so a crashed or
timed-out job resumes at the first unfinished task. Job metadata is persisted
to ``.lou-op/metadata.json`` in the work repo.
"""

from __future__ import annotations

import queue
import threading
import time
import uuid
from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, wait
from pathlib import Path
from typing import Callable, Dict, List, Optional

import yaml

from .backends.extractor import LLMClient
from .backends.raw_api import OpenRouterClient
from .backends.registry import get_backend
from .config import Settings
from .domain.graph import Node, TaskGraph
from .git_ops import log as git_log
from .judge import ConsistencyJudge
from .logutil import bind, bound, get_logger, register_queue, unregister_queue
from .loop import run_task
from .models import JobSpec, JobState, JobStatus, Task, TaskStatus
from .adapters.store_sqlite import SqliteStore
from .adapters.workspace_docker import DockerWorkspace
from .adapters.workspace_host import HostWorkspace
from .ports.workspace import Workspace as TreeWorkspace
from .validators import build_command_validators, build_validators
from .workspace import GitWorkspace, NullWorkspace, Workspace

log = get_logger()


class DependencyError(Exception):
    """Raised when task dependencies cannot be satisfied."""

    pass


def validate_tasks(tasks: List[Task]) -> None:
    """Reject unverifiable tasks at load.

    Only success_criteria and lint GATE the pass/fail loop; the consistency
    judge is advisory (it never blocks), so judge-only tasks would auto-pass
    with zero checks. Opt out per-task with ``allow_no_validators``."""
    for task in tasks:
        if not task.success_criteria and not task.lint and not task.allow_no_validators:
            hint = (
                " (judge: true is advisory-only and does not gate)"
                if task.judge
                else ""
            )
            raise ValueError(
                f"task '{task.name}' has no gating validators — no"
                f" success_criteria and lint: false{hint}. Its result would"
                " be meaningless. Add one or set allow_no_validators: true."
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
            settings.jobs_dir,
            remote=spec.git_remote,
            project_path=project_path,
            base_branch=settings.base_branch,
        )
    if key == "null":
        return NullWorkspace(settings.jobs_dir)
    raise ValueError(f"unknown workspace_type: {spec.workspace_type!r}")


def ready_tasks(tasks: List[Task]) -> List[Task]:
    """All tasks runnable right now — a thin shim over the pure
    ``domain.graph.TaskGraph`` (P7): readiness/ordering decisions are
    domain logic; this just maps Task objects in and out."""
    by_name = {t.name: t for t in tasks}
    known = [t for t in tasks if all(d in by_name for d in t.depends_on)]
    graph = TaskGraph([Node(t.name, tuple(t.depends_on)) for t in known])
    status = {t.name: t.status.value for t in known}
    ordered = graph.ready(status)
    # preserve declaration order (graph.ready sorts resumable first; the
    # scheduler relies on list order for serial reproduction)
    order_index = {t.name: i for i, t in enumerate(tasks)}
    return [by_name[n] for n in sorted(ordered, key=lambda n: order_index[n])]


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


def _make_tree(kind: str, root: Path, *, network: bool = False) -> TreeWorkspace:
    """Workspace factory: which locus executes model-influenced commands."""
    key = (kind or "host").strip().lower()
    if key == "host":
        return HostWorkspace(root)
    if key == "docker":
        return DockerWorkspace(root, network=network)
    if key == "modal":
        from .adapters.workspace_modal import ModalWorkspace  # lazy: modal SDK

        return ModalWorkspace(root, network=network)
    raise ValueError(f"unknown runtime: {kind!r} (host | docker | modal)")


def _tree_shell(tree: TreeWorkspace):
    """Adapt Workspace.exec to the validators' shell_fn(cmd, cwd, timeout)."""

    def shell(command: str, cwd: Path, *, timeout: int = 300):
        return tree.exec(command, timeout=timeout)

    return shell


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
        # durable event log (I3): survives the process, folds to RunState
        self.store = SqliteStore(self.settings.jobs_dir / "events.db")
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
        # let the structlog queue-sink route this job's log lines here, so any
        # thread only has to bind job_id for its output to reach the stream
        register_queue(job_id, self._log_queues[job_id])
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
            unregister_queue(state.job_id)
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
        # every log line in this job thread carries job_id → the queue-sink
        # routes it to this job's live stream (CLI drain / SSE)
        bind(job_id=state.job_id)
        log.debug("workspace", phase="orchestrator", type=spec.workspace_type)
        workspace = _make_workspace(spec, self.settings)
        workspace.setup(state.job_id, state.git_branch)
        log.info("workspace ready", phase="orchestrator", path=str(workspace.path))

        backend = get_backend(spec.backend, self.settings)
        log.debug("backend created", phase="orchestrator", backend=spec.backend)

        tasks = list(spec.tasks)

        # ONE working tree per job (I1): guards, validators, and the
        # agent's tools all flow through this Workspace. "Sandboxed" is a
        # property of the tree, not a bolt-on.
        runtime_kind = spec.runtime or self.settings.runtime
        tree = _make_tree(
            runtime_kind,
            workspace.path,
            network=self.settings.sandbox_network,
        )
        tree.setup(state.job_id)
        log.debug("tree ready", phase="orchestrator", runtime=runtime_kind)

        max_parallel = max(1, self.settings.max_parallel)
        if max_parallel > 1 and spec.workspace_type != "null":
            # one git working tree cannot host concurrent index operations;
            # per-task clones/worktrees are the v-next unlock
            raise ValueError(
                "--max-parallel > 1 currently requires workspace_type: null"
                " (git workspaces share one working tree)"
            )

        state_lock = threading.Lock()
        # behavior tests of tasks that have passed — every later task must keep
        # them green (regression net). Guarded by state_lock.
        passed_criteria: List[str] = []
        # job wall-clock ceiling (JobSpec.timeout_seconds, default 2h)
        job_deadline = time.monotonic() + max(60, spec.timeout_seconds)

        tree.commit(
            "freeze spec files before task loop",
            "lou-op <lou-op@sudohnim.dev>",
        )

        backend.use_workspace(tree)  # model tools run on the same tree
        log.info("starting tasks", phase="orchestrator", count=len(tasks))

        def run_one(task: Task) -> bool:
            # ThreadPoolExecutor does not copy contextvars into workers, so
            # re-bind job_id (+ task) here for this worker's log routing
            with bound(job_id=state.job_id, task=task.name):
                if time.monotonic() >= job_deadline:
                    return False  # over the ceiling: fail fast, don't start
                with state_lock:
                    state.current_task = task.name
                    regression = list(passed_criteria)  # prior tasks' behavior tests
                shell = _tree_shell(tree)
                timeout_s = self.settings.inference_timeout_s
                # acceptance = this task's behavior test + every prior task's
                # (regression net: a later task can't silently break an earlier)
                validators = build_validators(
                    task, timeout_s, shell_fn=shell
                ) + build_command_validators(regression, timeout_s, shell_fn=shell)
                # structural gate that DRIVES the build phase (compile/typecheck)
                build_checks = build_command_validators(
                    task.build_check, timeout_s, shell_fn=shell
                )
                consistency_judge = _make_consistency_judge(task, self.settings)
                results = run_task(
                    workspace.path,
                    task,
                    backend,
                    workspace=workspace,
                    validators=validators,
                    build_checks=build_checks,
                    consistency_judge=consistency_judge,
                    budget=self.settings.context_budget_tokens,
                    timeout=timeout_s,
                    deadline=job_deadline,
                    tree=tree,
                )
                passed = bool(results) and results[-1].done
                if passed:
                    with state_lock:
                        passed_criteria.extend(task.success_criteria)
                for r in results:
                    self.store.append(
                        state.job_id,
                        "iteration",
                        {
                            "task": task.name,
                            "n": r.iteration,
                            "passed": r.passed,
                            "wrote_files": r.wrote_files,
                            "commit": r.commit,
                            # validator output is the single most useful thing
                            # for debugging; keep it in the log of record
                            "validators": [
                                {
                                    "name": v.name,
                                    "status": v.status.value,
                                    "output": v.output[-2000:],
                                }
                                for v in r.validations
                            ],
                        },
                        version=2,
                    )
                # terminal reason for the whole task (last record carries it)
                self.store.append(
                    state.job_id,
                    "task_finished",
                    {
                        "task": task.name,
                        "passed": passed,
                        "stop_reason": (
                            results[-1].stop_reason if results else "no_iterations"
                        ),
                    },
                )
                with state_lock:
                    state.commits.extend(r.commit for r in results)
                    if passed:
                        state.completed_tasks.append(task.name)
                return passed

        def on_status(task: Task) -> None:
            # status writeback after every transition — crash-safe resume
            if tasks_path is not None:
                write_tasks(tasks_path, tasks)
            self.store.append(
                state.job_id,
                "task_status",
                {"task": task.name, "status": task.status.value},
            )
            # checkpoint the fold: long runs resume snapshot-bounded
            folded, _ = self.store.load(state.job_id)
            self.store.save_snapshot(state.job_id, folded)

        self.store.append(
            state.job_id, "run_created", {"tasks": [t.name for t in tasks]}
        )
        self.store.append(state.job_id, "run_started", {})
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
            self.store.append(
                state.job_id,
                "run_finished",
                {"status": "failed", "error": state.error},
            )
            return
        finally:
            tree.teardown()

        if any(t.status == TaskStatus.FAILED for t in tasks):
            failed = [t.name for t in tasks if t.status == TaskStatus.FAILED]
            state.status = JobStatus.FAILED
            state.error = f"task '{failed[0]}' did not pass"
            self.store.append(
                state.job_id,
                "run_finished",
                {"status": "failed", "error": state.error},
            )
            return

        state.current_task = None
        workspace.teardown(push_remote=bool(spec.git_remote))
        state.commits = git_log(workspace.path, count=len(state.commits) + 5)
        state.status = JobStatus.COMPLETED
        self.store.append(state.job_id, "run_finished", {"status": "completed"})
