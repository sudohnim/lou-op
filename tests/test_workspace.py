from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from lou_op.workspace import GitWorkspace, NullWorkspace


def test_git_workspace_setup_creates_git_dir(tmp_path: Path) -> None:
    ws = GitWorkspace(tmp_path)
    ws.setup("job1", "lou-op/job-1")
    assert (ws.path / ".git").exists()


def test_git_workspace_checkpoint_returns_sha(tmp_path: Path) -> None:
    ws = GitWorkspace(tmp_path)
    ws.setup("job2", "lou-op/job-2")
    (ws.path / "file.txt").write_text("hello")
    sha = ws.checkpoint("add file")
    assert sha and len(sha) >= 7  # short sha


def test_git_workspace_revert(tmp_path: Path) -> None:
    ws = GitWorkspace(tmp_path)
    ws.setup("job3", "lou-op/job-3")
    (ws.path / "a.txt").write_text("original")
    sha = ws.checkpoint("original")
    (ws.path / "a.txt").write_text("mutated")
    ws.checkpoint("mutation")
    ws.revert(sha)
    assert (ws.path / "a.txt").read_text() == "original"


def test_git_workspace_seeds_gitignore(tmp_path: Path) -> None:
    ws = GitWorkspace(tmp_path)
    ws.setup("job4", "lou-op/job-4")
    assert (ws.path / ".gitignore").exists()


def test_git_workspace_path_raises_before_setup(tmp_path: Path) -> None:
    ws = GitWorkspace(tmp_path)
    with pytest.raises(RuntimeError, match="not set up"):
        _ = ws.path


def test_null_workspace_setup_no_git(tmp_path: Path) -> None:
    ws = NullWorkspace(tmp_path)
    ws.setup("jobnull", "any-branch")
    assert ws.path.exists()
    assert not (ws.path / ".git").exists()


def test_null_workspace_checkpoint_noop(tmp_path: Path) -> None:
    ws = NullWorkspace(tmp_path)
    ws.setup("jobnull2", "branch")
    sha = ws.checkpoint("irrelevant message")
    assert sha == ""


def test_null_workspace_teardown_noop(tmp_path: Path) -> None:
    ws = NullWorkspace(tmp_path)
    ws.setup("jobnull3", "branch")
    ws.teardown(push_remote=True)  # must not raise


def test_null_workspace_path_raises_before_setup(tmp_path: Path) -> None:
    ws = NullWorkspace(tmp_path)
    with pytest.raises(RuntimeError, match="not set up"):
        _ = ws.path
