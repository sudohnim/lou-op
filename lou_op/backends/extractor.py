"""Optional SLM extractor: repair a big model's sloppy output into clean blocks.

Only used on the raw-API path. A cheap, small model re-reads the primary
model's text and re-emits well-formed ``<<<FILE>>>`` blocks. This reduces (but
cannot eliminate) format-parsing failures; the agent-CLI path avoids the
problem entirely.
"""

from __future__ import annotations

from typing import Protocol

from ..protocol import DONE_SENTINEL

_EXTRACT_INSTRUCTION = (
    "Re-emit the files described below as blocks of the exact form:\n"
    "<<<FILE path>>>\\n<content>\\n<<<END>>>\n"
    f"Preserve any {DONE_SENTINEL} token. Output only the blocks.\n\n"
)


class LLMClient(Protocol):
    def generate(self, prompt: str) -> str:
        ...


class SLMExtractor:
    def __init__(self, client: LLMClient) -> None:
        self.client = client

    def extract(self, text: str) -> str:
        return self.client.generate(_EXTRACT_INSTRUCTION + text)
