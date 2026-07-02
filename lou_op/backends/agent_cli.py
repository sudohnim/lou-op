"""Agent-CLI backend: supervise a coding-agent CLI that writes files itself.

This is the default for real runs. Because the agent edits files directly with
its own tools, there is no text-format-parsing failure mode. lou-op spawns it,
streams output under a watchdog, and detects the completion sentinel.
"""

from __future__ import annotations

import json

from ..exec import run_streaming
from ..models import IterationContext, IterationOutput
from ..protocol import has_done_sentinel
from .base import Backend
from .providers import Provider


def _parse_result(stdout: str) -> tuple[bool, str]:
    """Return ``(done, summary)`` from stream-json output.

    Claude CLI emits a final ``{"type":"result","subtype":"success",
    "result":"..."}``; we use its ``result`` text as the (clean) summary
    rather than the raw JSON tail. Falls back to sentinel detection + a stdout
    tail when no JSON lines are found (e.g. Codex).
    """
    json_lines = 0
    for raw in stdout.splitlines():
        raw = raw.strip()
        if not raw:
            continue
        try:
            obj = json.loads(raw)
        except json.JSONDecodeError:
            continue
        json_lines += 1
        if isinstance(obj, dict) and obj.get("type") == "result":
            done = obj.get("subtype") == "success"
            summary = str(obj.get("result") or "").strip()
            return done, summary
    if json_lines == 0:
        tail = "\n".join(stdout.splitlines()[-20:])
        return has_done_sentinel(stdout), tail
    return False, ""


class AgentCLIBackend(Backend):
    name = "agent-cli"
    include_code = False  # the agent reads the repo itself
    raw_api = False

    def __init__(
        self,
        provider: Provider,
        *,
        total_timeout: int = 1800,
        silence_timeout: int = 300,
    ) -> None:
        self.provider = provider
        self.total_timeout = total_timeout
        self.silence_timeout = silence_timeout

    def run_iteration(self, ctx: IterationContext) -> IterationOutput:
        cmd = self.provider.build_command(ctx.prompt, str(ctx.repo_path))
        result = run_streaming(
            cmd,
            ctx.repo_path,
            total_timeout=self.total_timeout,
            silence_timeout=self.silence_timeout,
            on_line=ctx.on_line,
        )
        if result.timed_out:
            raise TimeoutError(f"agent CLI '{self.provider.name}' watchdog timeout")
        done, summary = _parse_result(result.stdout)
        if not summary:
            summary = "\n".join(result.stdout.splitlines()[-20:])
        return IterationOutput(done=done, summary=summary, log=result.stdout)
