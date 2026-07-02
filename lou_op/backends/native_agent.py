"""Native tool-loop backend: agentic execution against any OpenAI-compatible endpoint.

This is the air-gapped analogue of the agent-CLI path. Instead of shelling out
to a vendor CLI, lou-op itself runs the inner agent loop: the model emits
OpenAI-format tool calls (read_file / write_file / edit_file / bash / ...),
lou-op executes them locally under a path jail, feeds results back, and
repeats until the model stops calling tools or a budget is hit.

Works with any endpoint that speaks ``/v1/chat/completions`` with tools —
vLLM or SGLang serving GLM/Qwen/DeepSeek locally, OpenRouter, etc. The model
never needs network access and the repo never leaves the machine.
"""

from __future__ import annotations

import json
import subprocess
import time
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

import httpx

from ..models import IterationContext, IterationOutput
from ..protocol import DONE_SENTINEL, has_done_sentinel
from .base import Backend

# Tool results are truncated so a chatty command can't blow the context.
_MAX_TOOL_RESULT_CHARS = 20_000
_MAX_READ_CHARS = 50_000
_BASH_TIMEOUT_S = 300

_SYSTEM_PROMPT = f"""\
You are an autonomous coding agent working in a git repository.
Use the provided tools to read code, write files, and run commands.
Work in small steps: inspect before you write, run the tests after you change code.

When every success criterion passes, reply with {DONE_SENTINEL} on its own line
and stop calling tools. Never claim done while tests are failing.
"""

_TOOLS: List[Dict[str, Any]] = [
    {
        "type": "function",
        "function": {
            "name": "read_file",
            "description": "Read a file's contents (relative path from repo root).",
            "parameters": {
                "type": "object",
                "properties": {"path": {"type": "string"}},
                "required": ["path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "write_file",
            "description": "Create or overwrite a file (relative path from repo root).",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string"},
                    "content": {"type": "string"},
                },
                "required": ["path", "content"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "edit_file",
            "description": (
                "Replace an exact, unique string in a file. Fails if old_string"
                " is missing or appears more than once."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string"},
                    "old_string": {"type": "string"},
                    "new_string": {"type": "string"},
                },
                "required": ["path", "old_string", "new_string"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "bash",
            "description": (
                "Run a shell command from the repo root (e.g. run tests)."
                f" Times out after {_BASH_TIMEOUT_S}s."
            ),
            "parameters": {
                "type": "object",
                "properties": {"command": {"type": "string"}},
                "required": ["command"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_dir",
            "description": "List a directory (relative path, default repo root).",
            "parameters": {
                "type": "object",
                "properties": {"path": {"type": "string", "default": "."}},
            },
        },
    },
]


def _jail(repo_path: Path, rel: str) -> Path:
    """Resolve ``rel`` under the repo root; refuse escapes (write_files rule)."""
    root = repo_path.resolve()
    target = (root / rel).resolve()
    if not str(target).startswith(str(root)):
        raise ValueError(f"path escapes repo: {rel}")
    return target


def _truncate(text: str, limit: int = _MAX_TOOL_RESULT_CHARS) -> str:
    if len(text) <= limit:
        return text
    return text[:limit] + f"\n... [truncated {len(text) - limit} chars]"


def execute_tool(repo_path: Path, name: str, args: Dict[str, Any]) -> str:
    """Run one tool call locally. Errors come back as text, never exceptions —
    the model should see its mistake and correct course."""
    try:
        if name == "read_file":
            target = _jail(repo_path, args["path"])
            return _truncate(target.read_text(encoding="utf-8"), _MAX_READ_CHARS)

        if name == "write_file":
            target = _jail(repo_path, args["path"])
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(args["content"], encoding="utf-8")
            return f"wrote {args['path']} ({len(args['content'])} chars)"

        if name == "edit_file":
            target = _jail(repo_path, args["path"])
            text = target.read_text(encoding="utf-8")
            old = args["old_string"]
            count = text.count(old)
            if count == 0:
                return "error: old_string not found in file"
            if count > 1:
                return f"error: old_string appears {count} times; must be unique"
            target.write_text(
                text.replace(old, args["new_string"], 1), encoding="utf-8"
            )
            return f"edited {args['path']}"

        if name == "bash":
            result = subprocess.run(
                args["command"],
                shell=True,
                cwd=repo_path,
                capture_output=True,
                text=True,
                timeout=_BASH_TIMEOUT_S,
            )
            out = (result.stdout + result.stderr).strip()
            return _truncate(f"exit {result.returncode}\n{out}")

        if name == "list_dir":
            target = _jail(repo_path, args.get("path", "."))
            entries = sorted(
                e.name + ("/" if e.is_dir() else "") for e in target.iterdir()
            )
            return "\n".join(entries) or "(empty)"

        return f"error: unknown tool {name}"
    except subprocess.TimeoutExpired:
        return f"error: command timed out after {_BASH_TIMEOUT_S}s"
    except (OSError, ValueError, KeyError) as exc:
        return f"error: {exc}"


# ``chat_fn(messages, tools) -> assistant message dict`` — injectable for tests.
ChatFn = Callable[[List[Dict[str, Any]], List[Dict[str, Any]]], Dict[str, Any]]


class NativeAgentBackend(Backend):
    """Inner agent loop over an OpenAI-compatible chat-completions endpoint."""

    name = "native"
    include_code = False  # the agent reads the repo itself
    raw_api = False

    def __init__(
        self,
        base_url: str,
        api_key: str,
        model_id: str,
        *,
        max_turns: int = 40,
        wall_timeout_s: int = 1800,
        request_timeout_s: int = 600,
        chat_fn: Optional[ChatFn] = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.model_id = model_id
        self.max_turns = max_turns
        self.wall_timeout_s = wall_timeout_s
        self.request_timeout_s = request_timeout_s
        self._chat_fn = chat_fn or self._http_chat

    def _http_chat(
        self, messages: List[Dict[str, Any]], tools: List[Dict[str, Any]]
    ) -> Dict[str, Any]:
        response = httpx.post(
            f"{self.base_url}/chat/completions",
            headers={"Authorization": f"Bearer {self.api_key}"},
            json={
                "model": self.model_id,
                "messages": messages,
                "tools": tools,
                "temperature": 0.2,
            },
            timeout=self.request_timeout_s,
        )
        response.raise_for_status()
        return response.json()["choices"][0]["message"]

    def run_iteration(self, ctx: IterationContext) -> IterationOutput:
        emit = ctx.on_line or (lambda _: None)
        messages: List[Dict[str, Any]] = [
            {"role": "system", "content": _SYSTEM_PROMPT},
            {"role": "user", "content": ctx.prompt},
        ]
        transcript: List[str] = []
        deadline = time.monotonic() + self.wall_timeout_s

        for turn in range(1, self.max_turns + 1):
            if time.monotonic() > deadline:
                raise TimeoutError(
                    f"native agent wall timeout ({self.wall_timeout_s}s)"
                )
            emit(f"[native] turn {turn}: calling {self.model_id} ...")
            msg = self._chat_fn(messages, _TOOLS)
            text = msg.get("content") or ""
            tool_calls = msg.get("tool_calls") or []

            if not tool_calls:
                emit(f"[native] final text ({len(text)} chars)")
                done = has_done_sentinel(text)
                summary = "; ".join(transcript[-10:]) or text[:500]
                return IterationOutput(done=done, summary=summary, log=text)

            # Assistant message must be echoed back verbatim for the endpoint
            # to associate the tool results that follow.
            messages.append(
                {"role": "assistant", "content": text, "tool_calls": tool_calls}
            )
            for call in tool_calls:
                fn = call.get("function", {})
                name = fn.get("name", "")
                try:
                    args = json.loads(fn.get("arguments") or "{}")
                except json.JSONDecodeError:
                    args = {}
                emit(f"[native] tool: {name}({_preview_args(args)})")
                result = execute_tool(ctx.repo_path, name, args)
                transcript.append(f"{name}: {result.splitlines()[0][:120]}")
                messages.append(
                    {
                        "role": "tool",
                        "tool_call_id": call.get("id", ""),
                        "content": result,
                    }
                )

        emit(f"[native] max turns ({self.max_turns}) reached without done signal")
        return IterationOutput(
            done=False,
            summary="; ".join(transcript[-10:]) or "max turns reached",
            log="\n".join(transcript),
        )


def _preview_args(args: Dict[str, Any]) -> str:
    parts = []
    for key, value in args.items():
        text = str(value).replace("\n", "\\n")
        parts.append(f"{key}={text[:60]!r}")
    return ", ".join(parts)
