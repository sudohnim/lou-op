"""Adapter-specific Workspace behavior (contract deltas from host)."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from tests.test_modal_runtime import fake_modal  # noqa: F401 - fixture


@pytest.fixture()
def repo(tmp_path: Path) -> Path:
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, capture_output=True)
    subprocess.run(
        ["git", "commit", "-q", "--allow-empty", "-m", "root"],
        cwd=tmp_path,
        capture_output=True,
    )
    return tmp_path


class TestModalWorkspace:
    def test_exec_syncs_around_command_internally(
        self, fake_modal, repo: Path  # noqa: F811
    ) -> None:
        """The A1 class dies here: every exec pushes the CURRENT host tree
        first and pulls results after — no caller-sequenced sync API."""
        from lou_op.adapters.workspace_modal import ModalWorkspace

        ws = ModalWorkspace(repo)
        ws.setup("job1")
        ws.write("guarded.py", "restored-content")
        ws.exec("pytest -q")
        # the tar shipped by exec's internal sync_in contains the file
        # written AFTER setup — proof exec pushes fresh state every time
        import io
        import tarfile

        shipped = fake_modal.files["/tmp/in.tar"]
        with tarfile.open(fileobj=io.BytesIO(shipped)) as tar:
            member = tar.extractfile("./guarded.py")
            assert member and member.read() == b"restored-content"
        # and the command itself ran wrapped in a hard-kill timeout (I8)
        joined = [" ".join(argv) for argv in fake_modal.execs]
        assert any("timeout -s KILL" in c and "pytest -q" in c for c in joined)

    def test_file_ops_are_host_side(self, fake_modal, repo: Path) -> None:  # noqa: F811
        from lou_op.adapters.workspace_modal import ModalWorkspace

        ws = ModalWorkspace(repo)
        ws.write("a.py", "x = 1\n")
        assert (repo / "a.py").read_text() == "x = 1\n"
        assert ws.read("a.py") == "x = 1\n"


class TestDockerWorkspace:
    def test_shares_host_tree_and_wraps_kill(self, repo: Path, monkeypatch) -> None:
        from lou_op.adapters.workspace_docker import DockerWorkspace

        ws = DockerWorkspace(repo)
        # file ops host-side (bind mount = same tree)
        ws.write("a.py", "x = 1\n")
        assert (repo / "a.py").read_text() == "x = 1\n"

        captured = {}

        def fake_shell(cmd, cwd, *, timeout):
            captured["cmd"] = cmd
            from lou_op.exec import CmdResult

            return CmdResult(0, "ok", "", False)

        monkeypatch.setattr(ws._runtime, "shell", fake_shell)
        res = ws.exec("pytest -q", timeout=60)
        assert res.passed
        assert captured["cmd"].startswith("timeout -s KILL 60 sh -c ")

    def test_kill_exit_codes_mark_killed(self, repo: Path, monkeypatch) -> None:
        from lou_op.adapters.workspace_docker import DockerWorkspace
        from lou_op.exec import CmdResult

        ws = DockerWorkspace(repo)
        monkeypatch.setattr(
            ws._runtime, "shell", lambda c, w, *, timeout: CmdResult(137, "", "", False)
        )
        res = ws.exec("sleep 999", timeout=1)
        assert res.killed and not res.passed
