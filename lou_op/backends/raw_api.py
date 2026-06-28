"""Raw-API backend: call a model for text, parse the file protocol, write.

Optionally routes the model's output through an SLM extractor first. Includes a
thin OpenRouter client; it is gated on an API key and not exercised by tests.
"""

from __future__ import annotations

from typing import List, Optional

import httpx

from ..models import IterationContext, IterationOutput
from ..protocol import has_done_sentinel, parse_files, write_files
from .base import Backend
from .extractor import LLMClient, SLMExtractor


class OpenRouterClient:
    """Minimal OpenRouter chat-completions client."""

    def __init__(
        self,
        api_key: str,
        model_id: str,
        base_url: str = "https://openrouter.ai/api/v1",
        timeout: int = 300,
    ) -> None:
        self.api_key = api_key
        self.model_id = model_id
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout

    def generate(self, prompt: str) -> str:
        response = httpx.post(
            f"{self.base_url}/chat/completions",
            headers={"Authorization": f"Bearer {self.api_key}"},
            json={
                "model": self.model_id,
                "messages": [{"role": "user", "content": prompt}],
                "temperature": 1.0,
            },
            timeout=self.timeout,
        )
        response.raise_for_status()
        data = response.json()
        content: str = data["choices"][0]["message"]["content"]
        return content


class RawAPIBackend(Backend):
    name = "raw-api"
    include_code = True
    raw_api = True

    def __init__(
        self,
        client: LLMClient,
        extractor: Optional[SLMExtractor] = None,
    ) -> None:
        self.client = client
        self.extractor = extractor

    def run_iteration(self, ctx: IterationContext) -> IterationOutput:
        text = self.client.generate(ctx.prompt)
        if self.extractor is not None:
            text = self.extractor.extract(text)
        files = parse_files(text)
        written: List[str] = write_files(ctx.repo_path, files)
        done = has_done_sentinel(text)
        summary = "Wrote: " + ", ".join(written) if written else "No files"
        return IterationOutput(done=done, summary=summary, log=text)
