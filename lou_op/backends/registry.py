"""Construct backends/providers by name from settings."""

from __future__ import annotations

from ..config import Settings
from .agent_cli import AgentCLIBackend
from .base import Backend
from .extractor import SLMExtractor
from .mock import MockBackend
from .native_agent import NativeAgentBackend
from .providers import ClaudeProvider, CodexProvider, Provider
from .raw_api import OpenRouterClient, RawAPIBackend


def get_provider(name: str, cli_path: str, model: str = "") -> Provider:
    key = name.strip().lower()
    if key == "claude":
        return ClaudeProvider(cli_path, model=model)
    if key == "codex":
        return CodexProvider(cli_path)
    raise ValueError(f"unknown agent provider: {name!r}")


def get_backend(name: str, settings: Settings) -> Backend:
    key = name.strip().lower()
    if key == "mock":
        return MockBackend()
    if key in ("agent-cli", "agent_cli", "agent"):
        provider = get_provider(
            settings.agent_provider, settings.agent_cli_path, settings.agent_model
        )
        return AgentCLIBackend(provider, silence_timeout=settings.silence_timeout_s)
    if key in ("raw-api", "raw_api", "raw"):
        if not settings.openrouter_api_key:
            raise ValueError("raw-api backend requires OPENROUTER_API_KEY")
        client = OpenRouterClient(
            settings.openrouter_api_key,
            settings.model_id,
            settings.openrouter_base_url,
            settings.inference_timeout_s,
        )
        extractor = None
        if settings.extractor_model_id:
            extractor_client = OpenRouterClient(
                settings.openrouter_api_key,
                settings.extractor_model_id,
                settings.openrouter_base_url,
                settings.inference_timeout_s,
            )
            extractor = SLMExtractor(extractor_client)
        return RawAPIBackend(client, extractor)
    if key == "native":
        if not settings.openrouter_api_key:
            raise ValueError("native backend requires OPENROUTER_API_KEY")
        return NativeAgentBackend(
            settings.openrouter_base_url,
            settings.openrouter_api_key,
            settings.model_id,
            auth_scheme=settings.auth_scheme,
            max_turns=settings.native_max_turns,
            wall_timeout_s=settings.native_wall_timeout_s,
            request_timeout_s=settings.inference_timeout_s,
        )
    raise ValueError(f"unknown backend: {name!r}")
