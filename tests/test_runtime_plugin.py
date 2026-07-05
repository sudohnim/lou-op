"""Spec (A1): runtime plugins + native bash routed through the runtime."""

from __future__ import annotations

from pathlib import Path

import pytest

from lou_op.exec import CmdResult
from lou_op.runtime import HostRuntime, Runtime, get_runtime, register_runtime


class FakeCloudRuntime(Runtime):
    """Captures every command; simulates a no-shared-FS sandbox."""

    def __init__(self, **kwargs) -> None:
        self.commands: list[str] = []
        self.synced_in = 0
        self.synced_out = 0

    def setup(self, job_id: str, repo_path: Path) -> None:
        pass

    def shell(self, command: str, cwd: Path, *, timeout: int = 300) -> CmdResult:
        self.commands.append(command)
        return CmdResult(0, "cloud-ok", "", False)

    def teardown(self) -> None:
        pass

    def sync_in(self, repo_path: Path) -> None:
        self.synced_in += 1

    def sync_out(self, repo_path: Path) -> None:
        self.synced_out += 1


class TestRegistry:
    def test_register_and_get(self) -> None:
        register_runtime("fake-cloud", FakeCloudRuntime)
        assert isinstance(get_runtime("fake-cloud"), FakeCloudRuntime)

    def test_unknown_lists_known(self) -> None:
        register_runtime("fake-cloud", FakeCloudRuntime)
        with pytest.raises(ValueError, match="fake-cloud"):
            get_runtime("nope")

    def test_sync_hooks_default_noop(self, tmp_path: Path) -> None:
        rt = HostRuntime()
        rt.sync_in(tmp_path)  # must not raise
        rt.sync_out(tmp_path)


class FakeCloudTree:
    """A Workspace fake for a no-shared-FS locus: records every exec."""

    def __init__(self, root: Path) -> None:
        from lou_op.adapters.workspace_host import HostWorkspace

        self._host = HostWorkspace(root)
        self.root = self._host.root
        self.commands: list[str] = []

    def __getattr__(self, name):  # tree/vcs ops delegate to host
        return getattr(self._host, name)

    def exec(self, command: str, *, timeout: int = 300, deadline=None):
        from lou_op.ports.workspace import ExecResult

        self.commands.append(command)
        return ExecResult(0, "cloud-ok", "")


class TestNativeToolsThroughWorkspace:
    """Refactor P1 (I1/I7): every native tool call flows through the job's
    ONE Workspace — no per-call sync choreography to forget."""

    def test_bash_routed_through_tree(self, tmp_path: Path) -> None:
        from lou_op.backends.native_agent import NativeAgentBackend

        tree = FakeCloudTree(tmp_path)
        script = iter(
            [
                {
                    "content": None,
                    "tool_calls": [
                        {
                            "id": "1",
                            "type": "function",
                            "function": {
                                "name": "bash",
                                "arguments": '{"command": "pytest -q"}',
                            },
                        }
                    ],
                },
                {"content": "<lou-done/>", "tool_calls": []},
            ]
        )
        backend = NativeAgentBackend(
            "http://localhost", "k", "m", chat_fn=lambda m, t: next(script)
        )
        backend.use_workspace(tree)
        from tests.test_native_agent import _ctx

        out = backend.run_iteration(_ctx(tmp_path))
        assert tree.commands == ["pytest -q"]  # ran on the tree, not host
        assert out.done

    def test_execute_tool_exec_output_shape(self, tmp_path: Path) -> None:
        from lou_op.backends.native_agent import execute_tool

        tree = FakeCloudTree(tmp_path)
        out = execute_tool(tree, "bash", {"command": "echo hi"})
        assert out.startswith("exit 0")
        assert "cloud-ok" in out

    def test_file_tools_use_the_same_tree(self, tmp_path: Path) -> None:
        from lou_op.backends.native_agent import execute_tool

        tree = FakeCloudTree(tmp_path)
        execute_tool(tree, "write_file", {"path": "a.py", "content": "x"})
        assert (tmp_path / "a.py").exists()
        assert tree.commands == []  # file ops need no exec


class TestOneTreeGuardsAndValidation:
    """The A1 bug class is structurally dead: guards and validators operate
    on the SAME Workspace object, so a validator can never grade a tree the
    guards did not produce."""

    def test_validator_sees_guard_restored_spec(self, tmp_path: Path) -> None:
        import subprocess

        from lou_op.adapters.workspace_host import HostWorkspace
        from lou_op.loop import run_task
        from lou_op.models import IterationContext, IterationOutput, Task
        from lou_op.models import ValidationResult

        subprocess.run(["git", "init", "-q"], cwd=tmp_path, capture_output=True)
        (tmp_path / "tests_spec.py").write_text("original spec\n")
        subprocess.run(["git", "add", "-A"], cwd=tmp_path, capture_output=True)
        subprocess.run(
            ["git", "commit", "-q", "-m", "seed"], cwd=tmp_path, capture_output=True
        )

        tree = HostWorkspace(tmp_path)

        class _TamperBackend:
            name = "tamper"
            include_code = False
            raw_api = False

            def run_iteration(self, ctx: IterationContext) -> IterationOutput:
                (ctx.repo_path / "tests_spec.py").write_text("tampered\n")
                (ctx.repo_path / "impl.py").write_text("x = 1\n")
                return IterationOutput(done=True, summary="Wrote: impl.py", log="")

            def use_workspace(self, t) -> None:
                pass

        class _SpecIntactViaTree:
            """Reads through the SAME tree the guards restored."""

            name = "spec-intact"

            def run(self, repo_path: Path) -> ValidationResult:
                ok = (repo_path / "impl.py").exists() and (
                    tree.read("tests_spec.py") == "original spec\n"
                )
                return ValidationResult(name=self.name, passed=ok, output="")

        task = Task(
            name="t",
            success_criteria=["true"],
            protected_files=["tests_spec.py"],
            max_iterations=1,
        )
        results = run_task(
            tmp_path,
            task,
            _TamperBackend(),
            validators=[_SpecIntactViaTree()],
            tree=tree,
        )
        assert results[-1].passed  # tamper restored BEFORE validation, one tree
