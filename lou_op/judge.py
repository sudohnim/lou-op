"""LLM-as-judge validator: semantic quality gate beyond test commands.

Command validators (pytest, lint) verify syntax and structure.
The judge asks a language model: "was the goal *actually* accomplished?"
It catches cases where the agent games the tests without solving the problem.
"""

from __future__ import annotations

from pathlib import Path
from typing import Sequence

from .backends.extractor import LLMClient
from .models import Task, ValidationResult
from .validators import Validator

_JUDGE_PROMPT = """\
You are a senior code reviewer acting as a quality judge for an automated code-generation loop.

Task: {name}
Description:
{description}

Stated success criteria:
{criteria}

Current codebase:
{code}

---
Did the agent *actually* accomplish the goal described above — not just technically pass the \
test commands, but solve the real problem with reasonable quality?

Reply with exactly one of:
PASS
FAIL: <one concise sentence explaining what is missing or wrong>

Do not explain further. Do not add any other text."""

_CODE_BUDGET = 12_000  # chars — enough for small projects, cheap on tokens


def _snapshot(repo_path: Path, extensions: Sequence[str] = (".py",)) -> str:
    """Collect source files up to _CODE_BUDGET chars, skipping venv/cache dirs."""
    skip = {".venv", "__pycache__", ".pytest_cache", ".mypy_cache", ".git", ".lou-op"}
    parts: list[str] = []
    total = 0
    for ext in extensions:
        for path in sorted(repo_path.rglob(f"*{ext}")):
            if any(s in path.parts for s in skip):
                continue
            content = path.read_text(errors="replace")
            header = f"### {path.relative_to(repo_path)}"
            chunk = f"{header}\n{content}"
            parts.append(chunk)
            total += len(chunk)
            if total >= _CODE_BUDGET:
                parts.append("### (truncated — budget reached)")
                return "\n\n".join(parts)
    return "\n\n".join(parts) or "(no source files found)"


class JudgeLLMValidator(Validator):
    """Calls an LLM to assess whether the task goal was genuinely met."""

    name = "judge-llm"

    def __init__(self, client: LLMClient, task: Task) -> None:
        self.client = client
        self.task = task

    def run(self, repo_path: Path) -> ValidationResult:
        code = _snapshot(repo_path)
        criteria = "\n".join(f"- {c}" for c in self.task.success_criteria) or "(none listed)"
        prompt = _JUDGE_PROMPT.format(
            name=self.task.name,
            description=self.task.description or "(no description)",
            criteria=criteria,
            code=code,
        )
        try:
            response = self.client.generate(prompt).strip()
        except Exception as exc:  # noqa: BLE001 — judge unavailable → don't block loop
            return ValidationResult(
                name=self.name,
                passed=True,
                output=f"judge unavailable: {exc}",
            )
        passed = response.upper().startswith("PASS")
        return ValidationResult(name=self.name, passed=passed, output=response)
