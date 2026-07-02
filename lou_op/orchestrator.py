"""Job orchestration: load tasks, run them sequentially, track + persist state.

Task status is written back to ``tasks.yaml`` after each task so a crashed or
timed-out job resumes at the first unfinished task. Job metadata is persisted
to ``.lou-op/metadata.json`` in the work repo.
"""

from __future__ import annotations

import queue
import threading
import uuid
from pathlib import Path
from typing import Dict, List, Optional

import yaml

from .backends.extractor import LLMClient
from .backends.raw_api import OpenRouterClient
from .backends.registry import get_backend
from .config import Settings
from .git_ops import log
from .judge import ConsistencyJudge
from .loop import run_task
from .models import JobSpec, JobState, JobStatus, Task, TaskStatus
from .validators import Validator, build_validators
from .workspace import GitWorkspace, NullWorkspace, Workspace


class DependencyError(Exception):
    """Raised when task dependencies cannot be satisfied."""

    pass


def load_tasks(path: Path) -> List[Task]:
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    raw = data.get("tasks", [])
    return [Task.model_validate(item) for item in raw]


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


def next_task(tasks: List[Task]) -> Optional[Task]:
    """First in-progress task (interrupted), else first not-yet-passed task."""
    for task in tasks:
        if task.status == TaskStatus.IN_PROGRESS:
            return task
    for task in tasks:
        if task.status not in (TaskStatus.PASSED,):
            return task
    return None


def select_next_task(tasks: List[Task]) -> Optional[Task]:
    """Return first PENDING task whose dependencies are satisfied.

    Raises DependencyError if:
    - A dependency is FAILED
    - A dependency name does not exist
    - A dependency cycle is detected

    Returns None if no task is PENDING.
    """
    task_by_name = {t.name: t for t in tasks}
    pending_tasks = [t for t in tasks if t.status == TaskStatus.PENDING]

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

        while True:
            task = select_next_task(tasks)
            if task is None:
                break
            state.current_task = task.name
            task.status = TaskStatus.IN_PROGRESS
            if tasks_path is not None:
                write_tasks(tasks_path, tasks)

            validators = build_validators(task, self.settings.inference_timeout_s)
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
                on_line=on_line,
            )
            state.commits.extend(r.commit for r in results)
            passed = bool(results) and results[-1].done

            task.status = TaskStatus.PASSED if passed else TaskStatus.FAILED
            if tasks_path is not None:
                write_tasks(tasks_path, tasks)
            if passed:
                state.completed_tasks.append(task.name)
            else:
                state.status = JobStatus.FAILED
                state.error = f"task '{task.name}' did not pass"
                return

        state.current_task = None
        workspace.teardown(push_remote=bool(spec.git_remote))
        state.commits = log(workspace.path, count=len(state.commits) + 5)
        state.status = JobStatus.COMPLETED
