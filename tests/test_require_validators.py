"""Spec (P0.4): a task with no validators is unverifiable — reject it at load.

Without success_criteria, lint, or judge, "passed" means nothing gated it.
Loading such a task must fail loudly unless explicitly opted out via
``allow_no_validators: true``.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from lou_op.models import JobSpec, Task
from lou_op.orchestrator import JobManager, load_tasks, validate_tasks


def _write_tasks(path: Path, tasks: list[dict]) -> Path:
    f = path / "tasks.yaml"
    f.write_text(yaml.safe_dump({"tasks": tasks}))
    return f


def test_load_rejects_validator_free_task(tmp_path: Path) -> None:
    f = _write_tasks(tmp_path, [{"name": "naked", "description": "no gates"}])
    with pytest.raises(ValueError, match="naked"):
        load_tasks(f)


def test_success_criteria_is_enough(tmp_path: Path) -> None:
    f = _write_tasks(tmp_path, [{"name": "ok", "success_criteria": ["pytest -q"]}])
    assert load_tasks(f)[0].name == "ok"


def test_lint_alone_is_enough(tmp_path: Path) -> None:
    f = _write_tasks(tmp_path, [{"name": "linty", "lint": True}])
    assert load_tasks(f)[0].name == "linty"


def test_judge_alone_is_enough(tmp_path: Path) -> None:
    f = _write_tasks(tmp_path, [{"name": "judged", "judge": True}])
    assert load_tasks(f)[0].name == "judged"


def test_explicit_optout_allows(tmp_path: Path) -> None:
    f = _write_tasks(tmp_path, [{"name": "naked", "allow_no_validators": True}])
    assert load_tasks(f)[0].name == "naked"


def test_error_names_only_offending_task(tmp_path: Path) -> None:
    f = _write_tasks(
        tmp_path,
        [
            {"name": "fine", "success_criteria": ["true"]},
            {"name": "naked-two", "description": "oops"},
        ],
    )
    with pytest.raises(ValueError, match="naked-two"):
        load_tasks(f)


def test_jobmanager_create_rejects_too(tmp_path: Path) -> None:
    """The API path (JobSpec, no yaml file) is gated by the same check."""
    spec = JobSpec(project_name="p", tasks=[Task(name="naked")])
    manager = JobManager()
    with pytest.raises(ValueError, match="naked"):
        manager.create(spec)


def test_validate_tasks_direct() -> None:
    validate_tasks([Task(name="ok", success_criteria=["true"])])
    with pytest.raises(ValueError, match="bad"):
        validate_tasks([Task(name="bad")])
