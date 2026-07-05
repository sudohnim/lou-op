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
import time
from typing import Any, Callable, Dict, List, Optional

from ..adapters.workspace_host import HostWorkspace
from ..audit import AuditLog
from ..ports.provider import Provider
from ..models import IterationContext, IterationOutput
from ..ports.workspace import Workspace, WorkspaceError
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


def _truncate(text: str, limit: int = _MAX_TOOL_RESULT_CHARS) -> str:
    if len(text) <= limit:
        return text
    return text[:limit] + f"\n... [truncated {len(text) - limit} chars]"


def execute_tool(tree: Workspace, name: str, args: Dict[str, Any]) -> str:
    """Run one tool call against the job's Workspace. Errors come back as
    text, never exceptions — the model should see its mistake and correct
    course.

    The Workspace owns the path jail, the scrubbed env, and the execution
    locus (host/docker/modal) — this function has no world access of its
    own (I1/I7)."""
    try:
        if name == "read_file":
            return _truncate(tree.read(args["path"]), _MAX_READ_CHARS)

        if name == "write_file":
            tree.write(args["path"], args["content"])
            return f"wrote {args['path']} ({len(args['content'])} chars)"

        if name == "edit_file":
            try:
                tree.edit(args["path"], args["old_string"], args["new_string"])
            except WorkspaceError as exc:
                return f"error: {exc}"
            return f"edited {args['path']}"

        if name == "bash":
            res = tree.exec(args["command"], timeout=_BASH_TIMEOUT_S)
            if res.killed:
                return f"error: command timed out after {_BASH_TIMEOUT_S}s"
            out = (res.stdout + res.stderr).strip()
            return _truncate(f"exit {res.returncode}\n{out}")

        if name == "list_dir":
            entries = tree.list(args.get("path", "."))
            return "\n".join(entries) or "(empty)"

        return f"error: unknown tool {name}"
    except (OSError, ValueError, KeyError, WorkspaceError) as exc:
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
        auth_scheme: str = "Bearer",
        max_turns: int = 40,
        wall_timeout_s: int = 1800,
        request_timeout_s: int = 600,
        max_job_tokens: int = 0,
        max_cost_usd: float = 0.0,
        price_in_per_mtok: float = 0.0,
        price_out_per_mtok: float = 0.0,
        chat_fn: Optional[ChatFn] = None,
        provider: Optional[Provider] = None,
        mode: str = "tools",
        extractor=None,
    ) -> None:
        from ..config import validate_base_url

        self.base_url = validate_base_url(base_url.rstrip("/"))
        self.api_key = api_key
        self.model_id = model_id
        # "Bearer" for OpenRouter/vLLM/Modal, "Api-Key" for Baseten.
        self.auth_scheme = auth_scheme
        self.max_turns = max_turns
        self.wall_timeout_s = wall_timeout_s
        self.request_timeout_s = request_timeout_s
        # cumulative across the whole job (backend instance is per-job);
        # 0 = unlimited
        self.max_job_tokens = max_job_tokens
        self.max_cost_usd = max_cost_usd
        self.price_in_per_mtok = price_in_per_mtok
        self.price_out_per_mtok = price_out_per_mtok
        self.tokens_used = 0
        self.prompt_tokens = 0
        self.completion_tokens = 0
        # all real inference flows through the Provider port (P4/I5);
        # chat_fn remains the deterministic test seam
        self._provider = provider
        self._chat_fn = chat_fn or self._provider_chat
        self._tree: Optional[Workspace] = None
        # capability mode (P5): "tools" needs endpoint tool_calls support;
        # "text" is the file-protocol fallback for endpoints without it
        # (vLLM w/o --enable-auto-tool-choice — the open-weight common case)
        if mode not in ("tools", "text"):
            raise ValueError(f"unknown agent mode: {mode!r} (tools | text)")
        self.mode = mode
        self.capability = "tool-calls" if mode == "tools" else "text-protocol"
        self.raw_api = mode == "text"  # loop builds the file-protocol prompt
        self._extractor = extractor

    def use_workspace(self, tree: Workspace) -> None:
        # every tool call operates on the job's ONE tree (I1)
        self._tree = tree

    def auth_header(self) -> Dict[str, str]:
        return {"Authorization": f"{self.auth_scheme} {self.api_key}"}

    @property
    def cost_usd(self) -> float:
        return (
            self.prompt_tokens * self.price_in_per_mtok
            + self.completion_tokens * self.price_out_per_mtok
        ) / 1_000_000

    def _over_budget(self) -> bool:
        if self.max_job_tokens and self.tokens_used >= self.max_job_tokens:
            return True
        return bool(self.max_cost_usd) and self.cost_usd >= self.max_cost_usd

    def _provider_chat(
        self, messages: List[Dict[str, Any]], tools: List[Dict[str, Any]]
    ) -> Dict[str, Any]:
        if self._provider is None:
            from ..adapters.provider_openai import OpenAICompatProvider

            self._provider = OpenAICompatProvider(
                self.base_url,
                self.api_key,
                self.model_id,
                auth_scheme=self.auth_scheme,
                timeout=self.request_timeout_s,
                price_in_per_mtok=self.price_in_per_mtok,
                price_out_per_mtok=self.price_out_per_mtok,
            )
        completion = self._provider.complete(messages, tools)
        # mirror the provider's cumulative accounting for budget checks
        self.tokens_used = self._provider.usage.total
        self.prompt_tokens = self._provider.usage.prompt_tokens
        self.completion_tokens = self._provider.usage.completion_tokens
        return completion.message

    def run_iteration(self, ctx: IterationContext) -> IterationOutput:
        tree = self._tree or HostWorkspace(ctx.repo_path)
        if self.mode == "text":
            return self._run_text_iteration(ctx, tree)
        emit = ctx.on_line or (lambda _: None)
        log = AuditLog(ctx.repo_path)
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
            if self._over_budget():
                emit(
                    "[native] budget exhausted"
                    f" (tokens {self.tokens_used}/{self.max_job_tokens or '∞'},"
                    f" ${self.cost_usd:.2f}/{self.max_cost_usd or '∞'}) — aborting"
                )
                log.record(
                    "budget_exceeded",
                    {
                        "tokens_used": self.tokens_used,
                        "token_cap": self.max_job_tokens,
                        "cost_usd": round(self.cost_usd, 4),
                        "cost_cap_usd": self.max_cost_usd,
                    },
                )
                return IterationOutput(
                    done=False,
                    summary=(
                        "aborted: budget exhausted"
                        f" (tokens {self.tokens_used}, ${self.cost_usd:.2f})"
                    ),
                    log="",
                )
            emit(f"[native] turn {turn}: calling {self.model_id} ...")
            msg = self._chat_fn(messages, _TOOLS)
            text = msg.get("content") or ""
            tool_calls = msg.get("tool_calls") or []

            if not tool_calls:
                emit(f"[native] final text ({len(text)} chars)")
                done = has_done_sentinel(text)
                summary = "; ".join(transcript[-10:]) or text[:500]
                log.record("usage", {"tokens_total": self.tokens_used})
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
                log.record("tool_call", {"name": name, **args})
                result = execute_tool(tree, name, args)
                # empty result (e.g. read of an empty file) has no lines
                first_line = result.splitlines()[0] if result else ""
                log.record("tool_result", {"name": name, "result": first_line})
                transcript.append(f"{name}: {first_line[:120]}")
                messages.append(
                    {
                        "role": "tool",
                        "tool_call_id": call.get("id", ""),
                        "content": result,
                    }
                )

        emit(f"[native] max turns ({self.max_turns}) reached without done signal")
        log.record("usage", {"tokens_total": self.tokens_used})
        return IterationOutput(
            done=False,
            summary="; ".join(transcript[-10:]) or "max turns reached",
            log="\n".join(transcript),
        )

    def _run_text_iteration(
        self, ctx: IterationContext, tree: Workspace
    ) -> IterationOutput:
        """Text file-protocol fallback (P5): one accounted completion, files
        parsed from the response and written THROUGH the tree (I1) — the
        old raw-api backend wrote to the host directly; this mode inherits
        the jail and locus for free."""
        from ..protocol import parse_files, parse_scratchpad

        emit = ctx.on_line or (lambda _: None)
        log = AuditLog(ctx.repo_path)
        emit(f"[native:text] calling {self.model_id} ...")
        msg = self._chat_fn([{"role": "user", "content": ctx.prompt}], [])
        text = msg.get("content") or ""
        if self._extractor is not None:
            text = self._extractor.extract(text)
        files = parse_files(text)
        written: List[str] = []
        for file in files:
            tree.write(file.path, file.content)
            log.record("tool_call", {"name": "write_file", "path": file.path})
            written.append(file.path)
        emit(f"[native:text] wrote {len(written)} file(s): {written}")
        done = has_done_sentinel(text)
        if done and not written:
            src = [
                p
                for p in ctx.repo_path.rglob("*")
                if p.is_file()
                and not p.name.startswith(".")
                and ".lou-op" not in str(p)
            ]
            if not src:
                emit("[native:text] done claimed but repo empty — continuing")
                done = False
        log.record("usage", {"tokens_total": self.tokens_used})
        summary = f"Wrote: {', '.join(written)}" if written else text[:500]
        return IterationOutput(
            done=done,
            summary=summary,
            log=text,
            scratchpad=parse_scratchpad(text),
        )


def _preview_args(args: Dict[str, Any]) -> str:
    parts = []
    for key, value in args.items():
        text = str(value).replace("\n", "\\n")
        parts.append(f"{key}={text[:60]!r}")
    return ", ".join(parts)
