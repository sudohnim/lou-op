"""Shared fixtures: an isolated git repo and configured git identity."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from lou_op.git_ops import ensure_repo


@pytest.fixture()
def repo(tmp_path: Path) -> Path:
    path = tmp_path / "work"
    ensure_repo(path)
    # commit author is set per-commit; nothing else needed here.
    subprocess.run(["git", "checkout", "-B", "main"], cwd=path, check=True)
    return path
