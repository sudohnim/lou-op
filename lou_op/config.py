"""Runtime settings, sourced from the environment with sensible defaults.

Nothing here hardcodes a model name (the PRD's "GLM-5.2" is not a real model):
the backend is pluggable and the default is the deterministic ``mock`` backend,
so the whole loop runs with zero API keys.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

# Load .env from cwd or nearest parent directory.
# override=False means real env vars always win over .env values.
load_dotenv(override=False)


def _env(name: str, default: str) -> str:
    value = os.getenv(name)
    return value if value else default


def _env_int(name: str, default: int) -> int:
    value = os.getenv(name)
    try:
        return int(value) if value else default
    except ValueError:
        return default


@dataclass
class Settings:
    """Process-wide configuration."""

    # Which backend drives iterations: "mock" | "agent-cli" | "raw-api" | "native".
    default_backend: str = "mock"

    # agent-cli backend.
    agent_provider: str = "claude"
    agent_cli_path: str = "claude"
    agent_model: str = ""  # empty => CLI default; e.g. "haiku" to pin cheapest

    # raw-api + native backends (any OpenAI-compatible endpoint).
    model_id: str = "z-ai/glm-4.6"
    openrouter_api_key: str = ""
    openrouter_base_url: str = "https://openrouter.ai/api/v1"
    extractor_model_id: str = ""  # empty => no SLM extractor stage

    # native backend budgets.
    native_max_turns: int = 40
    native_wall_timeout_s: int = 1800

    # Loop budgets / safeguards.
    context_budget_tokens: int = 100_000
    inference_timeout_s: int = 300
    silence_timeout_s: int = 300

    # Where job working repos live.
    jobs_dir: Path = Path(".lou-op-jobs")

    @classmethod
    def from_env(cls) -> "Settings":
        return cls(
            default_backend=_env("LOU_BACKEND", "mock"),
            agent_provider=_env("LOU_AGENT_PROVIDER", "claude"),
            agent_model=_env("LOU_AGENT_MODEL", ""),
            agent_cli_path=_env("LOU_AGENT_CLI_PATH", "claude"),
            model_id=_env("LOU_MODEL_ID", "z-ai/glm-4.6"),
            openrouter_api_key=_env("OPENROUTER_API_KEY", ""),
            openrouter_base_url=_env(
                "LOU_OPENROUTER_BASE_URL", "https://openrouter.ai/api/v1"
            ),
            extractor_model_id=_env("LOU_EXTRACTOR_MODEL_ID", ""),
            native_max_turns=_env_int("LOU_NATIVE_MAX_TURNS", 40),
            native_wall_timeout_s=_env_int("LOU_NATIVE_WALL_TIMEOUT", 1800),
            context_budget_tokens=_env_int("LOU_CONTEXT_BUDGET", 100_000),
            inference_timeout_s=_env_int("LOU_INFERENCE_TIMEOUT", 300),
            silence_timeout_s=_env_int("LOU_SILENCE_TIMEOUT", 300),
            jobs_dir=Path(_env("LOU_JOBS_DIR", ".lou-op-jobs")),
        )
