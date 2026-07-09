"""Spec (v3-B1/B4): PRD markdown → frozen-spec task graph."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from lou_op.prd import decompose_prd, materialize_specs

_MODEL_JSON = {
    "tasks": [
        {
            "name": "slugify",
            "description": "implement slugify",
            "spec_path": "tests/test_slug.py",
            "spec_content": "from slug import slugify\n\n\ndef test_x():\n    assert slugify('A B') == 'a-b'\n",
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
    # frozen: the test is protected (content restored every iteration) — the
    # ONLY constraint on the implementer; no file-scope fence
    assert task.protected_files == ["tests/test_slug.py"]


def test_probe_environment_reports_os_and_available_tools() -> None:
    """The probe gives the spec model ground truth about the machine so it
    writes gate commands that actually run (e.g. python3 vs python)."""
    from lou_op.prd import probe_environment

    env = probe_environment()
    assert "OS:" in env
    assert "AVAILABLE commands:" in env


def test_success_criteria_required(tmp_path: Path) -> None:
    """No silent Node/vitest default: a spec without a gate is rejected so the
    spec model must state, per stack, how the task is verified."""
    spec = dict(_MODEL_JSON["tasks"][0])
    del spec["success_criteria"]
    with pytest.raises(ValueError, match="no success_criteria"):
        materialize_specs([spec], tmp_path)
