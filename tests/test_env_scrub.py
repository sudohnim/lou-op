"""Spec (P0.2): model-facing subprocesses must not inherit API keys.

The agent's bash tool, `success_criteria` shells, and the agent-cli subprocess
all run model-influenced commands — a prompt-injected `echo $OPENROUTER_API_KEY`
must come back empty. Provider HTTP calls keep the key (in-process, not env).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from lou_op.backends.native_agent import execute_tool
from lou_op.exec import run_shell, run_streaming, scrubbed_env

SECRETS = {
    "OPENROUTER_API_KEY": "sk-secret",
    "ANTHROPIC_API_KEY": "sk-ant-secret",
    "LOU_MODEL_ID": "some-model",
    "LOU_AGENT_MODEL": "haiku",
}


@pytest.fixture(autouse=True)
def _secrets_in_env(monkeypatch: pytest.MonkeyPatch):
    for key, value in SECRETS.items():
        monkeypatch.setenv(key, value)


class TestScrubbedEnv:
    def test_removes_api_keys_and_lou_vars(self) -> None:
        env = scrubbed_env()
        for key in SECRETS:
            assert key not in env

    def test_keeps_normal_vars(self) -> None:
        env = scrubbed_env()
        assert "PATH" in env
        assert "HOME" in env

    def test_passthrough_wins(self) -> None:
        env = scrubbed_env(passthrough=["LOU_MODEL_ID"])
        assert env["LOU_MODEL_ID"] == "some-model"
        assert "OPENROUTER_API_KEY" not in env


class TestSubprocessesAreScrubbed:
    def test_run_shell_does_not_leak(self, tmp_path: Path) -> None:
        result = run_shell('echo "key=$OPENROUTER_API_KEY"', tmp_path)
        assert result.passed
        assert "sk-secret" not in result.stdout

    def test_run_streaming_does_not_leak(self, tmp_path: Path) -> None:
        result = run_streaming(
            ["sh", "-c", 'echo "key=$ANTHROPIC_API_KEY"'],
            tmp_path,
            total_timeout=30,
            silence_timeout=30,
        )
        assert "sk-ant-secret" not in result.stdout

    def test_native_bash_tool_does_not_leak(self, tmp_path: Path) -> None:
        out = execute_tool(
            tmp_path, "bash", {"command": 'echo "key=$OPENROUTER_API_KEY"'}
        )
        assert "sk-secret" not in out

    def test_run_shell_env_override_respected(self, tmp_path: Path) -> None:
        """Callers may pass an explicit env (e.g. with a passthrough)."""
        env = scrubbed_env(passthrough=["LOU_MODEL_ID"])
        result = run_shell('echo "m=$LOU_MODEL_ID"', tmp_path, env=env)
        assert "some-model" in result.stdout
