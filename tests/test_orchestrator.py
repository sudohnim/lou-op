from __future__ import annotations

from pathlib import Path

from lou_op.config import Settings
from lou_op.models import JobSpec, JobStatus, Task, TaskStatus
from lou_op.orchestrator import JobManager, load_tasks, select_next_task, write_tasks


def test_next_task_selection():
    tasks = [
        Task(name="a", status=TaskStatus.PASSED),
        Task(name="b", status=TaskStatus.PENDING),
        Task(name="c", status=TaskStatus.PENDING),
    ]
    assert select_next_task(tasks).name == "b"


def test_next_task_prefers_in_progress():
    tasks = [
        Task(name="a", status=TaskStatus.PENDING),
        Task(name="b", status=TaskStatus.IN_PROGRESS),
    ]
    assert select_next_task(tasks).name == "b"


def test_next_task_none_when_all_passed():
    tasks = [Task(name="a", status=TaskStatus.PASSED)]
    assert select_next_task(tasks) is None


def test_tasks_roundtrip_writeback(tmp_path: Path):
    path = tmp_path / "tasks.yaml"
    tasks = [Task(name="a", success_criteria=["true"])]
    write_tasks(path, tasks)
    loaded = load_tasks(path)
    assert loaded[0].name == "a"
    assert loaded[0].success_criteria == ["true"]


def test_job_runs_to_completion_with_mock(tmp_path: Path):
    settings = Settings(jobs_dir=tmp_path / "jobs")
    manager = JobManager(settings)
    spec = JobSpec(
        project_name="demo",
        backend="mock",
        tasks=[Task(name="Calc", success_criteria=["python -m pytest -q"])],
    )
    state = manager.create(spec, run_async=False)
    assert state.status == JobStatus.COMPLETED
    assert "Calc" in state.completed_tasks
    assert state.commits


def test_writeback_marks_passed(tmp_path: Path):
    settings = Settings(jobs_dir=tmp_path / "jobs")
    tasks_path = tmp_path / "tasks.yaml"
    tasks = [Task(name="Calc", success_criteria=["python -m pytest -q"])]
    write_tasks(tasks_path, tasks)
    manager = JobManager(settings)
    spec = JobSpec(project_name="demo", backend="mock", tasks=tasks)
    manager.create(spec, run_async=False, tasks_path=tasks_path)
    reloaded = load_tasks(tasks_path)
    assert reloaded[0].status == TaskStatus.PASSED
