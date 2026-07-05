"""OpenAICompatProvider: one HTTP implementation for every OpenAI-shape
endpoint — OpenRouter, Baseten, vLLM/SGLang on Modal, local ollama.

Vendor differences are CONFIG, not code: base_url + auth_scheme ("Bearer"
vs Baseten's "Api-Key") + prices. Retries with backoff, base_url https
validation, and usage/cost accumulation are centralized here (P4/I5) —
adding a vendor is a config value.
"""

from __future__ import annotations

import time
from typing import Any, Dict, List, Optional

import httpx

from ..config import validate_base_url
from ..ports.provider import Completion, Provider, Usage

_RETRY_DELAYS = (0.0, 5.0, 15.0)


class OpenAICompatProvider(Provider):
    def __init__(
        self,
        base_url: str,
        api_key: str,
        model_id: str,
        *,
        auth_scheme: str = "Bearer",
        timeout: int = 300,
        temperature: float = 0.2,
        price_in_per_mtok: float = 0.0,
        price_out_per_mtok: float = 0.0,
        retries: int = 3,
    ) -> None:
        self.base_url = validate_base_url(base_url.rstrip("/"))
        self.api_key = api_key
        self.model_id = model_id
        self.auth_scheme = auth_scheme
        self.timeout = timeout
        self.temperature = temperature
        self.price_in = price_in_per_mtok
        self.price_out = price_out_per_mtok
        self.retries = max(1, retries)
        self.usage = Usage()
        self.cost_usd = 0.0

    def auth_header(self) -> Dict[str, str]:
        return {"Authorization": f"{self.auth_scheme} {self.api_key}"}

    def _post(self, payload: dict) -> dict:
        last: Optional[BaseException] = None
        for attempt in range(self.retries):
            try:
                response = httpx.post(
                    f"{self.base_url}/chat/completions",
                    headers=self.auth_header(),
                    json=payload,
                    timeout=self.timeout,
                )
                response.raise_for_status()
                return response.json()
            except (httpx.HTTPError, ValueError) as exc:
                last = exc
                delay = _RETRY_DELAYS[min(attempt, len(_RETRY_DELAYS) - 1)]
                if delay and attempt < self.retries - 1:
                    time.sleep(delay)
        assert last is not None
        raise last

    def complete(
        self,
        messages: List[Dict[str, Any]],
        tools: Optional[List[Dict[str, Any]]] = None,
    ) -> Completion:
        payload: Dict[str, Any] = {
            "model": self.model_id,
            "messages": messages,
            "temperature": self.temperature,
        }
        if tools:
            payload["tools"] = tools
        data = self._post(payload)
        raw_usage = data.get("usage") or {}
        usage = Usage(
            prompt_tokens=int(raw_usage.get("prompt_tokens") or 0),
            completion_tokens=int(raw_usage.get("completion_tokens") or 0),
        )
        cost = (
            usage.prompt_tokens * self.price_in
            + usage.completion_tokens * self.price_out
        ) / 1_000_000
        # cumulative accounting — structural, not per-caller diligence (I5)
        self.usage.prompt_tokens += usage.prompt_tokens
        self.usage.completion_tokens += usage.completion_tokens
        self.cost_usd += cost
        return Completion(
            message=data["choices"][0]["message"], usage=usage, cost_usd=cost
        )
