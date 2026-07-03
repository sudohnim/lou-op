"""Endpoint smoke test: one real tool-calling round-trip, no job, no repo.

Answers the make-or-break question before you spend credits on a job:
does this endpoint accept our auth AND return a well-formed ``tool_calls``?
A model that can't emit tool calls silently breaks the native loop, so this
checks it explicitly rather than letting a job fail six iterations deep.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Dict, List, Optional

import httpx

from .backends.native_agent import NativeAgentBackend

# A trivial forced tool call — every tool-capable endpoint should manage this.
_PING_TOOLS: List[Dict[str, Any]] = [
    {
        "type": "function",
        "function": {
            "name": "report_ready",
            "description": "Call this to confirm you can emit tool calls.",
            "parameters": {
                "type": "object",
                "properties": {"ok": {"type": "boolean"}},
                "required": ["ok"],
            },
        },
    }
]
_PING_MESSAGES: List[Dict[str, Any]] = [
    {
        "role": "user",
        "content": "Call the report_ready tool with ok=true. Do not reply in text.",
    }
]


@dataclass
class PingResult:
    reachable: bool
    tool_calls_ok: bool
    detail: str
    raw: Optional[Dict[str, Any]] = None

    @property
    def ok(self) -> bool:
        return self.reachable and self.tool_calls_ok

    def render(self) -> str:
        mark = "✓" if self.ok else "✗"
        lines = [f"{mark} {self.detail}"]
        if self.raw is not None:
            preview = json.dumps(self.raw, indent=2)[:800]
            lines.append(preview)
        return "\n".join(lines)


def ping(backend: NativeAgentBackend) -> PingResult:
    """Make one real request through ``backend`` and classify the outcome."""
    try:
        msg = backend._chat_fn(_PING_MESSAGES, _PING_TOOLS)
    except httpx.HTTPStatusError as exc:
        code = exc.response.status_code
        hint = {
            401: "auth rejected — check API key and LOU_AUTH_SCHEME"
            " (Baseten wants Api-Key, not Bearer)",
            403: "forbidden — key valid but not permitted for this model",
            404: "not found — check base URL path (needs the /v1 segment?)",
            429: "rate limited — endpoint reachable, retry later",
        }.get(code, f"HTTP {code}")
        body = exc.response.text[:300]
        return PingResult(True, False, f"{hint}\n{body}")
    except httpx.HTTPError as exc:
        return PingResult(False, False, f"unreachable — {type(exc).__name__}: {exc}")
    except (KeyError, IndexError, ValueError) as exc:
        return PingResult(
            True, False, f"reached endpoint but response shape unexpected: {exc}"
        )

    tool_calls = msg.get("tool_calls") or []
    if not tool_calls:
        return PingResult(
            True,
            False,
            "reached, auth OK, but NO tool_calls returned — this model/endpoint"
            " does not support tool calling (native backend will not work)."
            " For vLLM add --enable-auto-tool-choice --tool-call-parser.",
            raw=msg,
        )

    fn = tool_calls[0].get("function", {})
    try:
        json.loads(fn.get("arguments") or "{}")
    except json.JSONDecodeError:
        return PingResult(
            True,
            False,
            "tool_calls returned but arguments is not valid JSON — parser"
            " mismatch for this model.",
            raw=msg,
        )

    return PingResult(
        True, True, f"OK — endpoint returns well-formed tool_calls ({fn.get('name')})"
    )
