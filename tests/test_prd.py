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
            "shared_files": ["package.json"],
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
    # frozen: the test is protected (content restored every iteration)
    assert task.protected_files == ["tests/test_slug.py"]
    # impl file is writable
    assert "slug.py" in task.allowed_paths
    # spec IS in allowed_paths so the guard doesn't delete/recreate it each
    # iteration — integrity comes from protected_files, not scope exclusion
    assert "tests/test_slug.py" in task.allowed_paths
    # shared scaffolding the spec model declared (deps) is writable too
    assert "package.json" in task.allowed_paths


def test_spec_stays_protected_even_though_in_scope(tmp_path: Path) -> None:
    """The exam is protected by content-restoration, not by scope exclusion:
    every spec file in allowed_paths must also be in protected_files."""
    tasks = build_tasks_from_prd("Build a slugifier.", tmp_path, _fake_generate)
    for task in tasks:
        for spec in task.protected_files:
            assert spec in task.allowed_paths  # in scope (no churn)
        # ...but protected, so _restore_protected rewrites it every iteration
        assert task.protected_files


def test_success_criteria_required(tmp_path: Path) -> None:
    """No silent Node/vitest default: a spec without a gate is rejected so the
    spec model must state, per stack, how the task is verified."""
    spec = dict(_MODEL_JSON["tasks"][0])
    del spec["success_criteria"]
    with pytest.raises(ValueError, match="no success_criteria"):
        materialize_specs([spec], tmp_path)


def test_present_manifest_is_guard_exempt_even_if_undeclared(tmp_path: Path) -> None:
    """Robustness over model compliance: a manifest that exists in the repo is
    always in allowed_paths, even when the spec omits it from shared_files."""
    (tmp_path / "go.mod").write_text("module x\n", encoding="utf-8")
    spec = dict(_MODEL_JSON["tasks"][0])
    spec.pop("shared_files", None)  # model forgot to declare it
    tasks = materialize_specs([spec], tmp_path)
    assert "go.mod" in tasks[0].allowed_paths
