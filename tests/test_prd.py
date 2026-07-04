"""Spec (v3-B1/B4): PRD markdown → frozen-spec task graph."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from lou_op.prd import (
    build_tasks_from_prd,
    decompose_prd,
    materialize_specs,
)

_MODEL_JSON = {
    "tasks": [
        {
            "name": "slugify",
            "description": "implement slugify",
            "spec_path": "tests/test_slug.py",
            "spec_content": "from slug import slugify\n\n\ndef test_x():\n    assert slugify('A B') == 'a-b'\n",
            "impl_paths": ["slug.py"],
            "success_criteria": ["python -m pytest tests/test_slug.py -q"],
        }
    ]
}


def _fake_generate(_prompt: str) -> str:
    return json.dumps(_MODEL_JSON)


def test_decompose_parses_tasks() -> None:
    specs = decompose_prd("Build a slugifier.", _fake_generate)
    assert specs[0]["name"] == "slugify"


def test_decompose_strips_markdown_fences() -> None:
    def fenced(_p: str) -> str:
        return "```json\n" + json.dumps(_MODEL_JSON) + "\n```"

    assert decompose_prd("x", fenced)[0]["name"] == "slugify"


def test_empty_decomposition_raises() -> None:
    with pytest.raises(ValueError, match="no tasks"):
        decompose_prd("x", lambda p: '{"tasks": []}')


def test_materialize_writes_and_freezes_specs(tmp_path: Path) -> None:
    tasks = materialize_specs(_MODEL_JSON["tasks"], tmp_path)
    # spec written to disk
    assert (tmp_path / "tests/test_slug.py").exists()
    task = tasks[0]
    # frozen: the test is protected and NOT in the writable scope
    assert task.protected_files == ["tests/test_slug.py"]
    assert task.allowed_paths == ["slug.py"]
    assert "tests/test_slug.py" not in task.allowed_paths


def test_impl_cannot_be_in_scope_with_spec(tmp_path: Path) -> None:
    """The implementer's scope must never include its own exam."""
    tasks = build_tasks_from_prd("Build a slugifier.", tmp_path, _fake_generate)
    for task in tasks:
        assert not set(task.protected_files) & set(task.allowed_paths)


def test_success_criteria_defaulted_when_absent(tmp_path: Path) -> None:
    spec = dict(_MODEL_JSON["tasks"][0])
    del spec["success_criteria"]
    tasks = materialize_specs([spec], tmp_path)
    assert tasks[0].success_criteria == ["python -m pytest tests/test_slug.py -q"]
