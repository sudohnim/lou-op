from __future__ import annotations

from pathlib import Path

from lou_op.models import Task
from lou_op.validators import (
    CommandValidator,
    PythonLintValidator,
    build_validators,
)


def test_command_validator_pass_fail(repo: Path):
    assert CommandValidator("true").run(repo).passed
    result = CommandValidator("false").run(repo)
    assert not result.passed


def test_command_validator_captures_output(repo: Path):
    result = CommandValidator("echo hello && false").run(repo)
    assert "hello" in result.output
    assert not result.passed


def test_build_validators_includes_lint():
    task = Task(name="t", success_criteria=["pytest"], lint=True)
    validators = build_validators(task)
    assert any(isinstance(v, PythonLintValidator) for v in validators)
    assert len(validators) == 2
