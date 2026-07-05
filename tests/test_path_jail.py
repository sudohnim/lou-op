"""Spec (P0.5): the path jail must resist prefix-collision and symlink escapes.

The old check was ``str(target).startswith(str(root))`` — which lets
``/tmp/repo-evil`` pass for root ``/tmp/repo``. Use ``Path.is_relative_to``
after resolving, and reject writes whose final component is a symlink that
resolves outside the root.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from lou_op.adapters.workspace_host import HostWorkspace
from lou_op.backends.native_agent import execute_tool
from lou_op.models import FileWrite
from lou_op.protocol import write_files


@pytest.fixture
def root(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    return repo


class TestPrefixCollision:
    def test_sibling_dir_sharing_prefix_is_rejected(self, root: Path) -> None:
        """/tmp/x/repo-evil must not pass the jail for root /tmp/x/repo."""
        evil = root.parent / (root.name + "-evil")
        evil.mkdir()
        rel = f"../{root.name}-evil/pwned.py"
        out = execute_tool(
            HostWorkspace(root), "write_file", {"path": rel, "content": "x"}
        )
        assert out.startswith("error:")
        assert not (evil / "pwned.py").exists()

    def test_write_files_rejects_prefix_sibling(self, root: Path) -> None:
        evil = root.parent / (root.name + "2")
        evil.mkdir()
        with pytest.raises(ValueError):
            write_files(root, [FileWrite(f"../{root.name}2/pwned.py", "x")])
        assert not (evil / "pwned.py").exists()


class TestDotDotEscape:
    def test_parent_escape_rejected(self, root: Path) -> None:
        out = execute_tool(
            HostWorkspace(root), "write_file", {"path": "../evil.py", "content": "x"}
        )
        assert out.startswith("error:")
        assert not (root.parent / "evil.py").exists()

    def test_read_escape_rejected(self, root: Path) -> None:
        # content distinct from the filename — the error message legitimately
        # echoes the offending path, which must not fail the leak assertion
        (root.parent / "secret.txt").write_text("hunter2-contents")
        out = execute_tool(HostWorkspace(root), "read_file", {"path": "../secret.txt"})
        assert out.startswith("error:")
        assert "hunter2-contents" not in out


class TestSymlinkEscape:
    def test_symlink_inside_pointing_outside_rejected_for_write(
        self, root: Path
    ) -> None:
        outside = root.parent / "outside.txt"
        outside.write_text("original")
        os.symlink(outside, root / "link.txt")
        out = execute_tool(
            HostWorkspace(root), "write_file", {"path": "link.txt", "content": "pwned"}
        )
        assert out.startswith("error:")
        assert outside.read_text() == "original"

    def test_symlinked_dir_outside_rejected(self, root: Path) -> None:
        outside_dir = root.parent / "outside_dir"
        outside_dir.mkdir()
        os.symlink(outside_dir, root / "sneaky", target_is_directory=True)
        out = execute_tool(
            HostWorkspace(root),
            "write_file",
            {"path": "sneaky/pwned.py", "content": "x"},
        )
        assert out.startswith("error:")
        assert not (outside_dir / "pwned.py").exists()

    def test_dangling_symlink_outside_rejected(self, root: Path) -> None:
        os.symlink(root.parent / "nowhere.txt", root / "dangling.txt")
        out = execute_tool(
            HostWorkspace(root), "write_file", {"path": "dangling.txt", "content": "x"}
        )
        assert out.startswith("error:")
        assert not (root.parent / "nowhere.txt").exists()


class TestLegitPathsStillWork:
    def test_normal_write_and_read(self, root: Path) -> None:
        out = execute_tool(
            HostWorkspace(root),
            "write_file",
            {"path": "pkg/mod.py", "content": "x = 1\n"},
        )
        assert not out.startswith("error:")
        assert (root / "pkg" / "mod.py").read_text() == "x = 1\n"

    def test_symlink_inside_root_is_fine(self, root: Path) -> None:
        (root / "real.txt").write_text("hi")
        os.symlink(root / "real.txt", root / "alias.txt")
        out = execute_tool(HostWorkspace(root), "read_file", {"path": "alias.txt"})
        assert out == "hi"

    def test_write_files_normal(self, root: Path) -> None:
        written = write_files(root, [FileWrite("a/b.py", "y = 2\n")])
        assert written == ["a/b.py"]
