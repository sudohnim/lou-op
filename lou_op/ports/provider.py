"""The Provider port: ALL model inference flows through here (P4, I5).

Every ``Completion`` carries token usage and cost — accounting is
structural, not per-backend diligence. Retries, timeouts and auth live in
the adapter; the judge and every agent share this single path.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional


@dataclass
class Usage:
    prompt_tokens: int = 0
    completion_tokens: int = 0

    @property
    def total(self) -> int:
        return self.prompt_tokens + self.completion_tokens


@dataclass
class Completion:
    message: Dict[str, Any]  # OpenAI-shape assistant message
    usage: Usage = field(default_factory=Usage)
    cost_usd: float = 0.0

    @property
    def text(self) -> str:
        return self.message.get("content") or ""

    @property
    def tool_calls(self) -> List[Dict[str, Any]]:
        return self.message.get("tool_calls") or []


class Provider(ABC):
    """One vendor connection with cumulative accounting."""

    #: cumulative across the provider's lifetime (one instance per job)
    usage: Usage
    cost_usd: float

    @abstractmethod
    def complete(
        self,
        messages: List[Dict[str, Any]],
        tools: Optional[List[Dict[str, Any]]] = None,
    ) -> Completion:
        ...

    def generate(self, prompt: str) -> str:
        """Convenience for single-shot text callers (judge, extractor, PRD
        planner) — same accounted path as the agents."""
        completion = self.complete([{"role": "user", "content": prompt}])
        return completion.text
