"""Seeded spec: audit trail (US-101). Implement lou_op/audit.py to green this.

Every tool call and result in the native backend gets one JSONL line in
``.lou-op/audit.jsonl`` — the data-custody artifact for client work.
"""

from __future__ import annotations

import json
from pathlib import Path

from lou_op.audit import AuditLog
from lou_op.backends.native_agent import NativeAgentBackend
from lou_op.models import IterationContext, Task


def _read_events(repo: Path) -> list[dict]:
    path = repo / ".lou-op" / "audit.jsonl"
    return [json.loads(line) for line in path.read_text().splitlines()]


class TestAuditLog:
    def test_record_appends_jsonl(self, tmp_path: Path) -> None:
        log = AuditLog(tmp_path)
        log.record("tool_call", {"name": "bash", "command": "ls"})
        log.record("tool_result", {"name": "bash", "exit": 0})
        events = _read_events(tmp_path)
        assert len(events) == 2
        assert events[0]["event"] == "tool_call"
        assert events[0]["data"]["command"] == "ls"
        assert events[0]["ts"]  # ISO-8601 timestamp, non-empty

    def test_record_is_append_only(self, tmp_path: Path) -> None:
        log = AuditLog(tmp_path)
        log.record("a", {})
        AuditLog(tmp_path).record("b", {})  # new instance, same file
        assert [e["event"] for e in _read_events(tmp_path)] == ["a", "b"]


class TestNativeAgentAudit:
    def test_tool_calls_are_audited(self, repo: Path) -> None:
        """One tool_call + one tool_result event per executed tool."""
        turns = iter(
            [
                {
                    "content": None,
                    "tool_calls": [
                        {
                            "id": "1",
                            "type": "function",
                            "function": {
                                "name": "write_file",
                                "arguments": json.dumps(
                                    {"path": "a.py", "content": "x = 1\n"}
                                ),
                            },
                        }
                    ],
                },
                {"content": "<lou-done/>", "tool_calls": []},
            ]
        )
        backend = NativeAgentBackend(
            "http://localhost", "key", "m", chat_fn=lambda m, t: next(turns)
        )
        ctx = IterationContext(
            repo_path=repo, task=Task(name="t"), prompt="p", iteration=1, progress=""
        )
        backend.run_iteration(ctx)
        events = _read_events(repo)
        kinds = [e["event"] for e in events]
        assert "tool_call" in kinds and "tool_result" in kinds
        call = next(e for e in events if e["event"] == "tool_call")
        assert call["data"]["name"] == "write_file"
