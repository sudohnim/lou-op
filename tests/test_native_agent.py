"""Native tool-loop backend: scripted-endpoint tests (no HTTP)."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, List

from lou_op.backends.native_agent import NativeAgentBackend, execute_tool
from lou_op.models import IterationContext, Task


def _ctx(repo: Path) -> IterationContext:
    return IterationContext(
        repo_path=repo,
        task=Task(name="t"),
        prompt="make it pass",
        iteration=1,
        progress="",
    )


def _tool_call(call_id: str, name: str, **args: Any) -> Dict[str, Any]:
    return {
        "id": call_id,
        "type": "function",
        "function": {"name": name, "arguments": json.dumps(args)},
    }


def _scripted(turns: List[Dict[str, Any]]):
    """chat_fn that replays canned assistant messages in order."""
    remaining = list(turns)

    def chat(messages, tools):
        assert remaining, "backend called endpoint more times than scripted"
        return remaining.pop(0)

    return chat


def _backend(turns: List[Dict[str, Any]], **kwargs: Any) -> NativeAgentBackend:
    return NativeAgentBackend(
        "http://localhost", "key", "test-model", chat_fn=_scripted(turns), **kwargs
    )


class TestToolLoop:
    def test_write_then_done(self, repo: Path) -> None:
        backend = _backend(
            [
                {
                    "content": None,
                    "tool_calls": [
                        _tool_call("1", "write_file", path="a.py", content="x = 1\n")
                    ],
                },
                {"content": "all green\n<lou-done/>", "tool_calls": []},
            ]
        )
        output = backend.run_iteration(_ctx(repo))
        assert output.done is True
        assert (repo / "a.py").read_text() == "x = 1\n"
        assert "write_file" in output.summary

    def test_tool_results_fed_back(self, repo: Path) -> None:
        (repo / "notes.txt").write_text("hello from disk")
        seen: List[List[Dict[str, Any]]] = []

        def chat(messages, tools):
            seen.append([dict(m) for m in messages])
            if len(seen) == 1:
                return {
                    "content": None,
                    "tool_calls": [_tool_call("1", "read_file", path="notes.txt")],
                }
            return {"content": "<lou-done/>", "tool_calls": []}

        backend = NativeAgentBackend("http://localhost", "k", "m", chat_fn=chat)
        backend.run_iteration(_ctx(repo))
        # second request must contain the tool result with the file contents
        tool_msgs = [m for m in seen[1] if m["role"] == "tool"]
        assert len(tool_msgs) == 1
        assert "hello from disk" in tool_msgs[0]["content"]
        assert tool_msgs[0]["tool_call_id"] == "1"

    def test_no_done_sentinel_means_not_done(self, repo: Path) -> None:
        backend = _backend([{"content": "I think it works", "tool_calls": []}])
        output = backend.run_iteration(_ctx(repo))
        assert output.done is False

    def test_max_turns_cap(self, repo: Path) -> None:
        looping_turn = {
            "content": None,
            "tool_calls": [_tool_call("1", "list_dir")],
        }
        backend = _backend([dict(looping_turn) for _ in range(5)], max_turns=3)
        output = backend.run_iteration(_ctx(repo))
        assert output.done is False
        assert "list_dir" in output.summary

    def test_path_escape_reported_not_written(self, repo: Path) -> None:
        backend = _backend(
            [
                {
                    "content": None,
                    "tool_calls": [
                        _tool_call(
                            "1", "write_file", path="../evil.py", content="pwned"
                        )
                    ],
                },
                {"content": "<lou-done/>", "tool_calls": []},
            ]
        )
        backend.run_iteration(_ctx(repo))
        assert not (repo.parent / "evil.py").exists()


class TestExecuteTool:
    def test_bash_runs_in_repo(self, repo: Path) -> None:
        (repo / "hi.txt").write_text("x")
        result = execute_tool(repo, "bash", {"command": "ls"})
        assert result.startswith("exit 0")
        assert "hi.txt" in result

    def test_edit_requires_unique_match(self, repo: Path) -> None:
        (repo / "f.py").write_text("a = 1\na = 1\n")
        result = execute_tool(
            repo,
            "edit_file",
            {"path": "f.py", "old_string": "a = 1", "new_string": "b"},
        )
        assert "2 times" in result

    def test_edit_applies_once(self, repo: Path) -> None:
        (repo / "f.py").write_text("a = 1\n")
        execute_tool(
            repo,
            "edit_file",
            {"path": "f.py", "old_string": "a = 1", "new_string": "a = 2"},
        )
        assert (repo / "f.py").read_text() == "a = 2\n"

    def test_read_escape_blocked(self, repo: Path) -> None:
        result = execute_tool(repo, "read_file", {"path": "../../etc/passwd"})
        assert result.startswith("error:")

    def test_unknown_tool(self, repo: Path) -> None:
        assert execute_tool(repo, "rm_rf", {}).startswith("error: unknown tool")

    def test_errors_are_text_not_exceptions(self, repo: Path) -> None:
        result = execute_tool(repo, "read_file", {"path": "missing.py"})
        assert result.startswith("error:")


class TestTokenBudget:
    """Spec (C1): per-job token cap — abort cleanly, never run unbounded."""

    def test_over_budget_aborts_before_model_call(self, tmp_path):
        calls = []

        def chat(m, t):
            calls.append(1)
            return {"content": "<lou-done/>", "tool_calls": []}

        b = NativeAgentBackend(
            "http://localhost", "k", "m", max_job_tokens=100, chat_fn=chat
        )
        b.tokens_used = 100  # already at cap
        out = b.run_iteration(_ctx(tmp_path))
        assert not calls  # no provider call over budget
        assert not out.done
        assert "budget" in out.summary

    def test_zero_cap_means_unlimited(self, tmp_path):
        b = NativeAgentBackend(
            "http://localhost",
            "k",
            "m",
            max_job_tokens=0,
            chat_fn=lambda m, t: {"content": "<lou-done/>", "tool_calls": []},
        )
        b.tokens_used = 10_000_000
        out = b.run_iteration(_ctx(tmp_path))
        assert out.done  # ran normally

    def test_usage_recorded_to_audit(self, tmp_path):
        import json

        b = NativeAgentBackend(
            "http://localhost",
            "k",
            "m",
            chat_fn=lambda m, t: {"content": "<lou-done/>", "tool_calls": []},
        )
        b.tokens_used = 777
        b.run_iteration(_ctx(tmp_path))
        lines = (tmp_path / ".lou-op" / "audit.jsonl").read_text().splitlines()
        events = [json.loads(ln) for ln in lines]
        usage = [e for e in events if e["event"] == "usage"]
        assert usage and usage[-1]["data"]["tokens_total"] == 777
