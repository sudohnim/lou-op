"""Agent-CLI providers: how to invoke a coding-agent CLI headlessly.

A provider just knows how to build the argv for "run this prompt to completion
in this directory." Output parsing is intentionally minimal — the loop only
needs the combined text to look for the ``<lou-done/>`` sentinel.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import List


class Provider(ABC):
    name: str

    @abstractmethod
    def build_command(self, prompt: str, work_dir: str) -> List[str]: ...


_CLAUDE_ALLOWED_TOOLS = "Read,Write,Edit,Bash,Glob,Grep,LS"


class ClaudeProvider(Provider):
    """Claude Code headless with tool allowlist and stream-json output."""

    name = "claude"

    def __init__(
        self,
        cli_path: str = "claude",
        *,
        allowed_tools: str = _CLAUDE_ALLOWED_TOOLS,
    ) -> None:
        self.cli_path = cli_path
        self.allowed_tools = allowed_tools

    def build_command(self, prompt: str, work_dir: str) -> List[str]:  # noqa: ARG002
        return [
            self.cli_path,
            # Hermetic: load no MCP servers, so the agent doesn't pollute the
            # generated repo (e.g. serena writing .serena/) and starts faster.
            "--strict-mcp-config",
            "--allowedTools",
            self.allowed_tools,
            "-p",
            prompt,
            "--output-format",
            "stream-json",
            "--verbose",
        ]


class CodexProvider(Provider):
    """OpenAI Codex CLI headless: ``codex exec <prompt>``."""

    name = "codex"

    def __init__(self, cli_path: str = "codex") -> None:
        self.cli_path = cli_path

    def build_command(self, prompt: str, work_dir: str) -> List[str]:
        return [self.cli_path, "exec", prompt]
