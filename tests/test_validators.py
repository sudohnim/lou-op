from __future__ import annotations

from pathlib import Path

from lou_op.exec import strip_ansi
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


def test_command_validator_strips_ansi(repo: Path):
    # vitest/playwright color their output even when piped; the loop must
    # scrub \x1b[... sequences before feeding them to the model.
    cmd = r"printf '\x1b[31m\x1b[1mError\x1b[0m: boom' && false"
    result = CommandValidator(cmd).run(repo)
    assert not result.passed
    assert "\x1b" not in result.output
    assert "Error: boom" in result.output


def test_strip_ansi_handles_truecolor_and_osc():
    assert strip_ansi("\x1b[38;2;255;0;0mred\x1b[0m") == "red"
    assert strip_ansi("\x1b[38;5;208morange\x1b[0m") == "orange"
    assert strip_ansi("\x1b]8;;https://x.io\x07link\x1b]8;;\x07") == "link"
    assert strip_ansi("plain text") == "plain text"


def test_build_validators_includes_lint():
    task = Task(name="t", success_criteria=["pytest"], lint=True)
    validators = build_validators(task)
    assert any(isinstance(v, PythonLintValidator) for v in validators)
    assert len(validators) == 2
