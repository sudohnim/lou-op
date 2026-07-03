"""Endpoint smoke test: classification branches (no real HTTP)."""

from __future__ import annotations

import httpx

from lou_op.backends.native_agent import NativeAgentBackend
from lou_op.ping import ping


def _backend(chat_fn) -> NativeAgentBackend:
    return NativeAgentBackend("http://localhost", "k", "m", chat_fn=chat_fn)


def _tool_msg(name: str = "report_ready", arguments: str = '{"ok": true}') -> dict:
    return {
        "content": None,
        "tool_calls": [
            {
                "id": "1",
                "type": "function",
                "function": {"name": name, "arguments": arguments},
            }
        ],
    }


def test_well_formed_tool_call_is_ok() -> None:
    result = ping(_backend(lambda m, t: _tool_msg()))
    assert result.ok is True
    assert "well-formed tool_calls" in result.detail


def test_no_tool_calls_flagged() -> None:
    result = ping(_backend(lambda m, t: {"content": "sure!", "tool_calls": []}))
    assert result.reachable is True
    assert result.tool_calls_ok is False
    assert "does not support tool calling" in result.detail
    assert result.raw is not None  # raw echoed for debugging


def test_bad_json_arguments_flagged() -> None:
    result = ping(_backend(lambda m, t: _tool_msg(arguments="{not json")))
    assert result.ok is False
    assert "not valid JSON" in result.detail


def test_auth_error_gives_hint() -> None:
    def chat(m, t):
        raise httpx.HTTPStatusError(
            "401",
            request=httpx.Request("POST", "http://localhost"),
            response=httpx.Response(401, text="unauthorized"),
        )

    result = ping(_backend(chat))
    assert result.reachable is True  # server answered, just rejected us
    assert result.tool_calls_ok is False
    assert "auth rejected" in result.detail and "Api-Key" in result.detail


def test_unreachable_endpoint() -> None:
    def chat(m, t):
        raise httpx.ConnectError("no route to host")

    result = ping(_backend(chat))
    assert result.reachable is False
    assert "unreachable" in result.detail


def test_unexpected_shape_flagged() -> None:
    def chat(m, t):
        raise KeyError("choices")

    result = ping(_backend(chat))
    assert result.reachable is True
    assert "response shape unexpected" in result.detail


def test_auth_header_scheme_respected() -> None:
    b = NativeAgentBackend("http://localhost", "secret", "m", auth_scheme="Api-Key")
    assert b.auth_header() == {"Authorization": "Api-Key secret"}


def test_insecure_remote_base_url_rejected() -> None:
    import pytest as _pytest

    with _pytest.raises(ValueError, match="insecure base_url"):
        NativeAgentBackend("http://api.example.com/v1", "k", "m")


def test_loopback_http_allowed() -> None:
    NativeAgentBackend("http://localhost:11435/v1", "k", "m")  # ollama-style


def test_https_allowed() -> None:
    NativeAgentBackend("https://openrouter.ai/api/v1", "k", "m")
