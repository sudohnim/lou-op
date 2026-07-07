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

_TOOL_ENV = Path(__file__).resolve().parent.parent / ".env"


def load_project_env(project_dir: "Path | None" = None) -> None:
    """Populate os.environ from .env files, precedence highest → lowest:

        real shell env  >  <project_dir>/.env  >  ./ .env  >  tool-global .env

    Every load uses ``override=False``, and the *destination* project loads
    *before* the tool-global one — so a key in the project you're running
    (``lou-op run ~/proj/PRD.md`` → ``~/proj/.env``) wins over lou-op's own
    ``.env``, while a real shell variable still beats them all.

    Call once at the CLI/API entry, before ``Settings.from_env()``.
    """
    seen: set = set()
    candidates = []
    if project_dir is not None:
        candidates.append(Path(project_dir) / ".env")
    candidates.append(Path.cwd() / ".env")
    candidates.append(_TOOL_ENV)
    for env_file in candidates:
        try:
            resolved = env_file.resolve()
        except OSError:
            continue
        if resolved in seen or not env_file.exists():
            continue
        seen.add(resolved)
        load_dotenv(env_file, override=False)


def _env(name: str, default: str) -> str:
    value = os.getenv(name)
    return value if value else default


def _env_int(name: str, default: int) -> int:
    value = os.getenv(name)
    try:
        return int(value) if value else default
    except ValueError:
        return default


def _env_bool(name: str, default: bool = False) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in ("1", "true", "yes", "on")


def _env_float(name: str, default: float = 0.0) -> float:
    value = os.getenv(name)
    try:
        return float(value) if value else default
    except ValueError:
        return default


_LOOPBACK_HOSTS = {"localhost", "127.0.0.1", "::1", "0.0.0.0"}


def validate_base_url(url: str) -> str:
    """Refuse plain-http inference endpoints unless they're loopback.

    Prompts and code go to this endpoint; over cleartext http to a remote
    host they're interceptable. Local ollama/vLLM (localhost) is exempt.
    """
    from urllib.parse import urlparse

    parsed = urlparse(url)
    if parsed.scheme == "https":
        return url
    if parsed.scheme == "http" and parsed.hostname in _LOOPBACK_HOSTS:
        return url
    raise ValueError(
        f"insecure base_url {url!r}: use https:// (plain http is only"
        " allowed for loopback hosts like localhost)"
    )


@dataclass
class Settings:
    """Process-wide configuration."""

    # Which backend drives iterations: "mock" | "agent-cli" | "raw-api" | "native".
    default_backend: str = "mock"

    # agent-cli backend.
    agent_provider: str = "claude"
    agent_cli_path: str = "claude"
    agent_model: str = ""  # empty => CLI default; e.g. "haiku" to pin cheapest
    # model that authors specs from a PRD; empty => model_id. Set to a
    # STRONGER model than the implementer for verifier independence (B3).
    spec_model: str = ""

    # raw-api + native backends (any OpenAI-compatible endpoint).
    model_id: str = "z-ai/glm-4.6"
    openrouter_api_key: str = ""
    openrouter_base_url: str = "https://openrouter.ai/api/v1"
    extractor_model_id: str = ""  # empty => no SLM extractor stage

    # native backend budgets.
    native_max_turns: int = 40
    native_wall_timeout_s: int = 1800
    # "Bearer" (OpenRouter/vLLM/Modal) or "Api-Key" (Baseten).
    auth_scheme: str = "Bearer"
    # strict scope: tasks without allowed_paths get scope inferred from their
    # description instead of unlimited write access.
    strict_scope: bool = False
    # execution runtime for model-influenced commands: "host" | "docker".
    runtime: str = "host"
    # tasks with satisfied deps run concurrently up to this bound (1 = serial).
    max_parallel: int = 1
    # hard cap on total provider tokens per job (0 = unlimited).
    max_job_tokens: int = 0
    # hard cap on provider spend per job in USD (0 = unlimited). Needs the
    # per-mtok prices below to convert usage into dollars.
    max_cost_usd: float = 0.0
    price_in_per_mtok: float = 0.0
    price_out_per_mtok: float = 0.0
    # sandbox egress is DENY by default; opt in with LOU_SANDBOX_NETWORK=on
    # (only when the task legitimately needs package installs etc.).
    sandbox_network: bool = False

    # Loop budgets / safeguards.
    context_budget_tokens: int = 100_000
    inference_timeout_s: int = 300
    silence_timeout_s: int = 300
    # Output token cap for the PRD decomposition call. Decompositions embed
    # every task's spec file as JSON, so they run long — a low cap truncates
    # the JSON mid-string and parsing dies cryptically.
    spec_max_tokens: int = 32_000
    # Ref a job branch forks from when running in place on an existing repo.
    # Empty => auto-detect (origin/HEAD → main → master → HEAD). Keeps reruns
    # from stacking on each other's committed output.
    base_branch: str = ""

    # Where job working repos live.
    jobs_dir: Path = Path(".lou-op-jobs")

    @classmethod
    def from_env(cls) -> "Settings":
        return cls(
            default_backend=_env("LOU_BACKEND", "mock"),
            agent_provider=_env("LOU_AGENT_PROVIDER", "claude"),
            agent_model=_env("LOU_AGENT_MODEL", ""),
            spec_model=_env("LOU_SPEC_MODEL", ""),
            agent_cli_path=_env("LOU_AGENT_CLI_PATH", "claude"),
            model_id=_env("LOU_MODEL_ID", "z-ai/glm-4.6"),
            openrouter_api_key=_env("OPENROUTER_API_KEY", ""),
            openrouter_base_url=_env(
                "LOU_OPENROUTER_BASE_URL", "https://openrouter.ai/api/v1"
            ),
            extractor_model_id=_env("LOU_EXTRACTOR_MODEL_ID", ""),
            native_max_turns=_env_int("LOU_NATIVE_MAX_TURNS", 40),
            native_wall_timeout_s=_env_int("LOU_NATIVE_WALL_TIMEOUT", 1800),
            auth_scheme=_env("LOU_AUTH_SCHEME", "Bearer"),
            strict_scope=_env_bool("LOU_STRICT_SCOPE", False),
            runtime=_env("LOU_RUNTIME", "host"),
            max_parallel=_env_int("LOU_MAX_PARALLEL", 1),
            max_job_tokens=_env_int("LOU_MAX_JOB_TOKENS", 0),
            max_cost_usd=_env_float("LOU_MAX_COST_USD", 0.0),
            price_in_per_mtok=_env_float("LOU_PRICE_IN_PER_MTOK", 0.0),
            price_out_per_mtok=_env_float("LOU_PRICE_OUT_PER_MTOK", 0.0),
            sandbox_network=_env("LOU_SANDBOX_NETWORK", "off").lower() == "on",
            context_budget_tokens=_env_int("LOU_CONTEXT_BUDGET", 100_000),
            inference_timeout_s=_env_int("LOU_INFERENCE_TIMEOUT", 300),
            silence_timeout_s=_env_int("LOU_SILENCE_TIMEOUT", 300),
            spec_max_tokens=_env_int("LOU_SPEC_MAX_TOKENS", 32_000),
            base_branch=_env("LOU_BASE_BRANCH", ""),
            jobs_dir=Path(_env("LOU_JOBS_DIR", ".lou-op-jobs")),
        )
