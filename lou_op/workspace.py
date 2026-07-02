"""Workspace abstraction: git-backed or plain directory.

The loop only needs four operations from a workspace — setup, checkpoint,
revert, teardown — so non-git targets (data pipelines, analysis runs) can
plug in without touching the loop logic.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from pathlib import Path
from typing import Optional

from . import AUTHOR
from .git_ops import (
    checkout_branch,
    commit_all,
    ensure_repo,
    push,
    revert_to,
    seed_gitignore,
)


class Workspace(ABC):
    """Abstract workspace: a directory the Ralph loop reads and writes."""

    @property
    @abstractmethod
    def path(self) -> Path:
        """Local path agents and validators operate in."""

    @abstractmethod
    def setup(self, job_id: str, branch: str) -> None:
        """Prepare the working directory. Called once before the loop starts."""

    @abstractmethod
    def checkpoint(self, message: str) -> str:
        """Snapshot current state. Returns a checkpoint id (sha, name, or "")."""

    @abstractmethod
    def revert(self, checkpoint_id: str) -> None:
        """Roll back to a prior checkpoint."""

    @abstractmethod
    def teardown(self, *, push_remote: bool = False) -> None:
        """Optional cleanup / push after the loop finishes."""


class GitWorkspace(Workspace):
    """Git-backed workspace: branch and commit each iteration.

    If ``project_path`` is supplied the workspace operates in that existing
    repo directly — no sub-directory, no clone/init.  Otherwise a fresh repo
    is created under ``jobs_dir / job_id``.
    """

    def __init__(
        self,
        jobs_dir: Path,
        *,
        remote: Optional[str] = None,
        project_path: Optional[Path] = None,
    ) -> None:
        self._jobs_dir = jobs_dir
        self._remote = remote
        self._project_path = project_path
        self._path: Optional[Path] = None
        self._branch: str = ""

    @property
    def path(self) -> Path:
        if self._path is None:
            raise RuntimeError("GitWorkspace not set up — call setup() first")
        return self._path

    def setup(self, job_id: str, branch: str) -> None:
        self._branch = branch
        if self._project_path is not None:
            self._path = self._project_path
        else:
            self._path = self._jobs_dir / job_id
            ensure_repo(self._path, self._remote)
        checkout_branch(self._path, branch)
        seed_gitignore(self._path)

    def checkpoint(self, message: str) -> str:
        return commit_all(self.path, message, AUTHOR)

    def revert(self, checkpoint_id: str) -> None:
        revert_to(self.path, checkpoint_id)

    def teardown(self, *, push_remote: bool = False) -> None:
        if push_remote and self._remote:
            push(self.path, self._remote, self._branch)


class NullWorkspace(Workspace):
    """Plain directory workspace: no git, no checkpointing."""

    def __init__(self, jobs_dir: Path) -> None:
        self._jobs_dir = jobs_dir
        self._path: Optional[Path] = None

    @property
    def path(self) -> Path:
        if self._path is None:
            raise RuntimeError("NullWorkspace not set up — call setup() first")
        return self._path

    def setup(self, job_id: str, branch: str) -> None:  # noqa: ARG002
        self._path = self._jobs_dir / job_id
        self._path.mkdir(parents=True, exist_ok=True)

    def checkpoint(self, message: str) -> str:  # noqa: ARG002
        return ""

    def revert(self, checkpoint_id: str) -> None:  # noqa: ARG002
        pass

    def teardown(self, *, push_remote: bool = False) -> None:  # noqa: ARG002
        pass
