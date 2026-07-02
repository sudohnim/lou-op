"""Raw-API backend: call a model for text, parse the file protocol, write.

Optionally routes the model's output through an SLM extractor first. Includes a
thin OpenRouter client; it is gated on an API key and not exercised by tests.
"""

from __future__ import annotations

from typing import List, Optional

import httpx

from ..models import IterationContext, IterationOutput
from ..protocol import has_done_sentinel, parse_files, parse_scratchpad, write_files
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
                "temperature": 0.2,
                "max_tokens": 2048,
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
        emit = ctx.on_line or (lambda _: None)
        emit(f"[raw-api] calling {self.client.model_id} ...")
        text = self.client.generate(ctx.prompt)
        if self.extractor is not None:
            text = self.extractor.extract(text)
        emit(f"[raw-api] response ({len(text)} chars)")
        emit(f"[raw-api] preview: {text[:300]}")
        files = parse_files(text)
        written: List[str] = write_files(ctx.repo_path, files)
        emit(f"[raw-api] wrote {len(written)} file(s): {written}")
        done = has_done_sentinel(text)
        # reject done=True if no files were written this iteration and repo is empty
        if done and not written:
            src_files = [
                p for p in ctx.repo_path.rglob("*")
                if p.is_file() and not p.name.startswith(".") and ".lou-op" not in str(p)
            ]
            if not src_files:
                emit("[raw-api] done=True but no files exist — forcing another iteration")
                done = False
        emit(f"[raw-api] done={done}")
        scratchpad = parse_scratchpad(text)
        emit(f"[raw-api] scratchpad={'yes' if scratchpad else 'none'}")
        summary = "Wrote: " + ", ".join(written) if written else "No files"
        return IterationOutput(done=done, summary=summary, log=text, scratchpad=scratchpad)
