"""Raw-API backend: call a model for text, parse the file protocol, write.

Optionally routes the model's output through an SLM extractor first. Includes a
thin OpenRouter client; it is gated on an API key and not exercised by tests.
"""

from __future__ import annotations

from typing import Optional


class OpenRouterClient:
    """Single-shot text client for judge/extractor/PRD planning.

    Now a thin wrapper over the unified Provider port (P4) — usage and
    cost are accounted on the same path as the agents.
    """

    def __init__(
        self,
        api_key: str,
        model_id: str,
        base_url: str = "https://openrouter.ai/api/v1",
        timeout: int = 300,
        max_tokens: Optional[int] = None,
    ) -> None:
        from ..adapters.provider_openai import OpenAICompatProvider

        self.provider = OpenAICompatProvider(
            base_url, api_key, model_id, timeout=timeout, max_tokens=max_tokens
        )
        self.model_id = model_id

    def generate(self, prompt: str) -> str:
        return self.provider.generate(prompt)
