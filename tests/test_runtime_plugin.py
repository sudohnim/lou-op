"""Spec (A1): runtime plugins + native bash routed through the runtime."""

from __future__ import annotations

from pathlib import Path

import pytest

from lou_op.backends.native_agent import NativeAgentBackend, execute_tool
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


class TestNativeBashThroughRuntime:
    def test_bash_routed_and_synced(self, tmp_path: Path) -> None:
        rt = FakeCloudRuntime()
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
        backend.use_runtime(rt)
        from tests.test_native_agent import _ctx

        out = backend.run_iteration(_ctx(tmp_path))
        assert rt.commands == ["pytest -q"]  # ran in the sandbox, not host
        assert rt.synced_in == 1 and rt.synced_out == 1
        assert out.done

    def test_execute_tool_shell_fn_output_shape(self, tmp_path: Path) -> None:
        rt = FakeCloudRuntime()
        out = execute_tool(tmp_path, "bash", {"command": "echo hi"}, shell_fn=rt.shell)
        assert out.startswith("exit 0")
        assert "cloud-ok" in out

    def test_file_tools_stay_local(self, tmp_path: Path) -> None:
        """Only bash routes through the runtime; file tools are host-side
        (the host repo is the source of truth, synced around bash)."""
        rt = FakeCloudRuntime()
        execute_tool(
            tmp_path,
            "write_file",
            {"path": "a.py", "content": "x"},
            shell_fn=rt.shell,
        )
        assert (tmp_path / "a.py").exists()
        assert rt.commands == []


class TestGuardsSyncBeforeValidation:
    """Spec (v3-A1): on a no-shared-FS runtime, validators must see the
    guard-restored tree — not the model's tampered last push."""

    def test_sync_in_called_between_guards_and_validation(self, tmp_path: Path) -> None:
        import subprocess

        from lou_op.loop import run_task
        from lou_op.models import IterationContext, IterationOutput, Task
        from lou_op.models import ValidationResult

        subprocess.run(["git", "init", "-q"], cwd=tmp_path, capture_output=True)
        (tmp_path / "tests_spec.py").write_text("original spec\n")
        subprocess.run(["git", "add", "-A"], cwd=tmp_path, capture_output=True)
        subprocess.run(
            ["git", "commit", "-q", "-m", "seed"], cwd=tmp_path, capture_output=True
        )

        events: list[str] = []

        class _SpyRuntime(FakeCloudRuntime):
            def sync_in(self, repo_path: Path) -> None:
                # record what the spec file contained AT SYNC TIME
                events.append(
                    "sync:" + (repo_path / "tests_spec.py").read_text().strip()
                )

        class _TamperBackend:
            name = "tamper"
            include_code = False
            raw_api = False

            def run_iteration(self, ctx: IterationContext) -> IterationOutput:
                # model tampers with the protected spec
                (ctx.repo_path / "tests_spec.py").write_text("tampered\n")
                (ctx.repo_path / "impl.py").write_text("x = 1\n")
                return IterationOutput(done=True, summary="Wrote: impl.py", log="")

            def use_runtime(self, runtime) -> None:
                pass

        class _SpecMustBeOriginal:
            name = "spec-intact"

            def run(self, repo_path: Path) -> ValidationResult:
                events.append("validate")
                # red at preflight (impl.py absent) so the loop actually
                # runs; green only if impl exists AND spec is untampered
                ok = (repo_path / "impl.py").exists() and (
                    repo_path / "tests_spec.py"
                ).read_text() == "original spec\n"
                return ValidationResult(name=self.name, passed=ok, output="")

        rt = _SpyRuntime()
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
            validators=[_SpecMustBeOriginal()],
            runtime=rt,
        )
        # the sync that precedes validation must carry the RESTORED spec
        sync_events = [e for e in events if e.startswith("sync:")]
        assert sync_events, "runtime.sync_in never called before validation"
        assert sync_events[-1] == "sync:original spec"
        last_validate = len(events) - 1 - events[::-1].index("validate")
        assert events.index(sync_events[-1]) < last_validate
        assert results[-1].passed
