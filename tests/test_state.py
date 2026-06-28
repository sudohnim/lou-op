from __future__ import annotations

from pathlib import Path

from lou_op.models import Task
from lou_op.state import estimate_tokens, list_repo_files, render_state


def test_budget_excludes_oversized_files(repo: Path):
    (repo / "small.py").write_text("x = 1\n")
    (repo / "big.py").write_text("# " + "a" * 10_000 + "\n")
    task = Task(name="t", description="touch small.py")
    state = render_state(repo, task, budget=100, include_code=True)
    assert "small.py" in state
    assert "big.py" not in state


def test_mentioned_file_prioritized(repo: Path):
    (repo / "wanted.py").write_text("w = 1\n")
    task = Task(name="t", description="edit wanted.py please")
    files = list_repo_files(repo)
    assert "wanted.py" in files
    state = render_state(repo, task, budget=100_000, include_code=True)
    assert "wanted.py" in state


def test_agent_cli_prompt_omits_code(repo: Path):
    (repo / "secret.py").write_text("s = 1\n")
    task = Task(name="t")
    state = render_state(repo, task, include_code=False)
    assert "Current Codebase" not in state


def test_estimate_tokens():
    assert estimate_tokens("a" * 40) == 10
