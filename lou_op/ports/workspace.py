"""The Workspace port: owns the working tree — the ONLY way anything reads,
writes, or executes (invariant I1/I7).

Guards, validators, the agent's tools, and commits all go through one
Workspace bound to one locus (host dir, docker /work, modal sandbox).
"Sandboxed" is a property of the Workspace, not a bolt-on — so the A1 bug
class (guards restoring one tree while validators grade another) is
structurally impossible: there is only ever one tree.

Path jailing lives HERE, once. The twin implementations that previously
diverged (native_agent._jail vs protocol.write_files) are both retired in
favor of ``Workspace._resolve``.

``exec`` carries hard-cancel semantics (invariant I8): on deadline the
process group / container process is killed, never a cooperative flag.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Optional


class WorkspaceError(Exception):
    """Raised for path-jail violations and workspace misuse."""


@dataclass
class ExecResult:
    returncode: int
    stdout: str
    stderr: str
    timed_out: bool = False
    killed: bool = False  # hard-cancelled (deadline/cancel), not natural exit

    @property
    def passed(self) -> bool:
        return self.returncode == 0 and not self.timed_out and not self.killed


@dataclass
class Changed:
    """One changed path relative to the workspace root.

    ``status``: "modified" | "added" | "deleted" | "untracked" | "renamed".
    For renames ``old_path`` carries the source; ``path`` is the destination.
    """

    path: str
    status: str
    old_path: Optional[str] = None
    staged: bool = False


@dataclass
class Snapshot:
    ref: str
    untracked: List[str] = field(default_factory=list)


class Workspace(ABC):
    """One job's working tree + executor. All world access flows through it."""

    #: the host-side root of the tree (source of truth for file ops)
    root: Path

    # -- path jail (single implementation, shared by every adapter) --------

    def _resolve(self, rel: str) -> Path:
        """Resolve ``rel`` under root; refuse escapes.

        ``is_relative_to`` after ``resolve()`` — a plain prefix check would
        accept sibling dirs sharing the prefix (``/x/repo-evil`` for
        ``/x/repo``), and resolve() follows symlinks so a link pointing
        outside the tree resolves outside and is rejected.
        """
        root = self.root.resolve()
        target = (root / rel).resolve()
        if not target.is_relative_to(root):
            raise WorkspaceError(f"path escapes workspace: {rel}")
        return target

    # -- tree ---------------------------------------------------------------

    def read(self, rel: str) -> str:
        return self._resolve(rel).read_text(encoding="utf-8")

    def write(self, rel: str, content: str) -> None:
        target = self._resolve(rel)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")

    def delete(self, rel: str) -> None:
        self._resolve(rel).unlink()

    def list(self, rel: str = ".") -> List[str]:
        target = self._resolve(rel)
        return sorted(e.name + ("/" if e.is_dir() else "") for e in target.iterdir())

    def edit(self, rel: str, old: str, new: str) -> None:
        """Replace an exact, unique occurrence of ``old`` with ``new``."""
        target = self._resolve(rel)
        text = target.read_text(encoding="utf-8")
        count = text.count(old)
        if count == 0:
            raise WorkspaceError("old_string not found in file")
        if count > 1:
            raise WorkspaceError(f"old_string appears {count} times; must be unique")
        target.write_text(text.replace(old, new, 1), encoding="utf-8")

    # -- execution (adapter-specific: host process, docker exec, modal) ----

    @abstractmethod
    def exec(
        self,
        command: str,
        *,
        timeout: int = 300,
        deadline: Optional[float] = None,
    ) -> ExecResult:
        """Run a shell command inside the workspace locus.

        Hard-cancel semantics (I8): if ``deadline`` (time.monotonic) or
        ``timeout`` is breached, the process GROUP / container process is
        killed — a runaway child cannot outlive its budget. Environment is
        the strict allowlist (scrubbed), never the parent env.
        """

    # -- version control ----------------------------------------------------

    @abstractmethod
    def changed_paths(self) -> List[Changed]:
        """Uncommitted changes, parsed robustly (spaces, renames)."""

    @abstractmethod
    def restore_paths(self, paths: List[str]) -> None:
        """Restore tracked paths to HEAD; drop untracked/staged-only ones."""

    @abstractmethod
    def snapshot(self) -> Snapshot:
        """Capture current state so ``restore`` can return to it exactly."""

    @abstractmethod
    def restore(self, snap: Snapshot) -> None:
        """Return the tree to ``snap`` (tracked + untracked)."""

    @abstractmethod
    def commit(self, message: str, author: str) -> str:
        """Commit everything; return the commit id ('' if nothing to do)."""

    # -- lifecycle ----------------------------------------------------------

    def setup(self, job_id: str) -> None:  # noqa: B027 - optional hook
        """Prepare the locus (create container/sandbox). Host: no-op."""

    def teardown(self) -> None:  # noqa: B027 - optional hook
        """Destroy the locus. Host: no-op."""
