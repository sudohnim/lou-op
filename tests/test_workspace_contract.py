"""Workspace contract suite (refactor Phase 1).

One shared exam for every Workspace adapter — substitutability is the point
of the port. Host runs always; docker/modal reuse this class behind skips.

Security tier lives here too (jail escapes) because the jail is now ONE
implementation on the port, not per-backend copies.
"""

from __future__ import annotations

import subprocess
import time
from pathlib import Path

import pytest

from lou_op.adapters.workspace_host import HostWorkspace
from lou_op.ports.workspace import Workspace, WorkspaceError


@pytest.fixture()
def ws(tmp_path: Path) -> Workspace:
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, capture_output=True)
    subprocess.run(
        ["git", "commit", "-q", "--allow-empty", "-m", "root"],
        cwd=tmp_path,
        capture_output=True,
    )
    return HostWorkspace(tmp_path)


def _seed(ws: Workspace, rel: str, content: str = "seed\n") -> None:
    ws.write(rel, content)
    ws.commit(f"seed {rel}", "t <t@t>")


class TestTreeOps:
    def test_write_read_roundtrip(self, ws: Workspace) -> None:
        ws.write("a/b.py", "x = 1\n")
        assert ws.read("a/b.py") == "x = 1\n"

    def test_list_marks_dirs(self, ws: Workspace) -> None:
        ws.write("d/f.py", "")
        ws.write("top.py", "")
        entries = ws.list(".")
        assert "d/" in entries and "top.py" in entries

    def test_edit_unique_replacement(self, ws: Workspace) -> None:
        ws.write("f.py", "a = 1\nb = 2\n")
        ws.edit("f.py", "a = 1", "a = 99")
        assert "a = 99" in ws.read("f.py")

    def test_edit_rejects_ambiguous(self, ws: Workspace) -> None:
        ws.write("f.py", "x\nx\n")
        with pytest.raises(WorkspaceError, match="2 times"):
            ws.edit("f.py", "x", "y")

    def test_delete(self, ws: Workspace) -> None:
        ws.write("gone.py", "")
        ws.delete("gone.py")
        assert "gone.py" not in ws.list(".")


class TestJail:
    """Security tier: ONE jail, exercised at the port."""

    def test_parent_escape_rejected(self, ws: Workspace) -> None:
        with pytest.raises(WorkspaceError, match="escapes"):
            ws.read("../outside.txt")

    def test_absolute_path_rejected(self, ws: Workspace) -> None:
        with pytest.raises(WorkspaceError, match="escapes"):
            ws.write("/etc/passwd-like", "nope")

    def test_prefix_sibling_rejected(self, ws: Workspace, tmp_path: Path) -> None:
        evil = tmp_path.parent / (tmp_path.name + "-evil")
        evil.mkdir(exist_ok=True)
        (evil / "s.txt").write_text("hunter2-contents")
        with pytest.raises(WorkspaceError, match="escapes"):
            ws.read(f"../{evil.name}/s.txt")

    def test_symlink_escape_rejected(self, ws: Workspace, tmp_path: Path) -> None:
        outside = tmp_path.parent / f"{tmp_path.name}-target.txt"
        outside.write_text("hunter2-contents")
        (ws.root / "link.txt").symlink_to(outside)
        with pytest.raises(WorkspaceError, match="escapes"):
            ws.read("link.txt")


class TestExec:
    def test_basic_exec(self, ws: Workspace) -> None:
        res = ws.exec("echo hi")
        assert res.passed and "hi" in res.stdout

    def test_env_is_scrubbed(self, ws: Workspace, monkeypatch) -> None:
        monkeypatch.setenv("OPENROUTER_API_KEY", "sk-secret")
        monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "leak-me")
        res = ws.exec('echo "k=$OPENROUTER_API_KEY a=$AWS_SECRET_ACCESS_KEY"')
        assert "sk-secret" not in res.stdout
        assert "leak-me" not in res.stdout

    def test_runs_in_root(self, ws: Workspace) -> None:
        ws.write("here.txt", "x")
        res = ws.exec("ls")
        assert "here.txt" in res.stdout

    def test_deadline_hard_kills_stubborn_process_group(self, ws: Workspace) -> None:
        """I8: a child that ignores SIGTERM and spawns its own children must
        die within a bounded interval of the deadline — not run to completion."""
        start = time.monotonic()
        res = ws.exec(
            "trap '' TERM; sleep 30 & wait",
            timeout=300,
            deadline=time.monotonic() + 1.0,
        )
        elapsed = time.monotonic() - start
        assert res.killed and res.timed_out and not res.passed
        assert elapsed < 5.0  # bounded, nowhere near the 30s sleep

    def test_timeout_also_kills(self, ws: Workspace) -> None:
        res = ws.exec("sleep 30", timeout=1)
        assert res.killed and not res.passed


class TestVcs:
    def test_changed_paths_untracked_with_spaces(self, ws: Workspace) -> None:
        ws.write("my file.py", "junk")
        changes = {c.path: c.status for c in ws.changed_paths()}
        assert changes.get("my file.py") == "untracked"

    def test_changed_paths_rename_carries_old(self, ws: Workspace) -> None:
        _seed(ws, "a.txt", "original\n")
        subprocess.run(
            ["git", "mv", "a.txt", "b.txt"], cwd=ws.root, capture_output=True
        )
        renames = [c for c in ws.changed_paths() if c.status == "renamed"]
        assert renames and renames[0].path == "b.txt"
        assert renames[0].old_path == "a.txt"

    def test_restore_paths_tracked_and_untracked(self, ws: Workspace) -> None:
        _seed(ws, "keep.txt", "original\n")
        ws.write("keep.txt", "vandalized")
        ws.write("junk.txt", "junk")
        ws.restore_paths(["keep.txt", "junk.txt"])
        assert ws.read("keep.txt") == "original\n"
        assert "junk.txt" not in ws.list(".")

    def test_snapshot_restore_roundtrip(self, ws: Workspace) -> None:
        _seed(ws, "f.txt", "v1\n")
        snap = ws.snapshot()
        ws.write("f.txt", "v2\n")
        ws.write("new.txt", "junk")
        ws.commit("wip", "t <t@t>")
        ws.restore(snap)
        assert ws.read("f.txt") == "v1\n"
        assert "new.txt" not in ws.list(".")

    def test_commit_returns_sha_and_empty_when_clean(self, ws: Workspace) -> None:
        ws.write("f.txt", "x\n")
        sha = ws.commit("add f", "t <t@t>")
        assert len(sha) == 40
        assert ws.commit("noop", "t <t@t>") == ""
