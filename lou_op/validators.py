"""Validators run after each iteration; their pass/fail feeds the next loop.

``CommandValidator`` runs a ``success_criteria`` shell command.
``PythonLintValidator`` runs lou-op's own lint stack (black/isort/flake8/mypy)
over the generated project when a task sets ``lint: true``.
"""

from __future__ import annotations

import shutil
from abc import ABC, abstractmethod
from pathlib import Path
from typing import List

from .exec import run_command, run_shell
from .models import Task, ValidationResult, ValidationStatus


class Validator(ABC):
    name: str

    @abstractmethod
    def run(self, repo_path: Path) -> ValidationResult:
        ...


def _classify(result) -> ValidationStatus:
    """PASS/FAIL/ERROR for a shell result. ERROR = the gate could not execute
    at all (missing runner, timeout), so feeding it to the model is wasted
    turns — see ValidationStatus. Kept deliberately conservative: "no tests
    collected" is NOT an error, because a TDD loop writes the tests during
    iteration, so an empty gate before work is expected, not broken."""
    if result.passed:
        return ValidationStatus.PASS
    # 127 = command not found, 126 = found but not executable (POSIX shells)
    if result.timed_out or result.returncode in (126, 127):
        return ValidationStatus.ERROR
    if "command not found" in (result.stdout + result.stderr).lower():
        return ValidationStatus.ERROR
    return ValidationStatus.FAIL


class CommandValidator(Validator):
    """Run a single shell command; pass iff it exits 0.

    ``shell_fn`` lets a Runtime (e.g. docker sandbox) supply the executor;
    default is the host run_shell (scrubbed env)."""

    def __init__(self, command: str, timeout: int = 300, shell_fn=None) -> None:
        self.name = command
        self.command = command
        self.timeout = timeout
        self.shell_fn = shell_fn or run_shell

    def run(self, repo_path: Path) -> ValidationResult:
        result = self.shell_fn(self.command, repo_path, timeout=self.timeout)
        output = (result.stdout + result.stderr).strip()
        if result.timed_out:
            output = f"(timed out after {self.timeout}s)\n{output}"
        return ValidationResult(
            self.name, result.passed, output, status=_classify(result)
        )


class PythonLintValidator(Validator):
    """Run black/isort/flake8/mypy over the project (best-effort)."""

    name = "python-lint"
    _CHECKS = (
        ("black", ["black", "--check", "."]),
        ("isort", ["isort", "--check-only", "."]),
        ("flake8", ["flake8", "."]),
        ("mypy", ["mypy", "."]),
    )

    def __init__(self, timeout: int = 300) -> None:
        self.timeout = timeout

    def run(self, repo_path: Path) -> ValidationResult:
        outputs: List[str] = []
        passed = True
        for tool, cmd in self._CHECKS:
            if shutil.which(cmd[0]) is None:
                outputs.append(f"[skip] {tool} not installed")
                continue
            result = run_command(cmd, repo_path, timeout=self.timeout)
            mark = "PASS" if result.passed else "FAIL"
            outputs.append(
                f"[{mark}] {tool}\n{(result.stdout + result.stderr).strip()}"
            )
            passed = passed and result.passed
        return ValidationResult(self.name, passed, "\n".join(outputs))


def build_validators(task: Task, timeout: int = 300, shell_fn=None) -> List[Validator]:
    validators: List[Validator] = [
        CommandValidator(cmd, timeout, shell_fn) for cmd in task.success_criteria
    ]
    if task.lint:
        validators.append(PythonLintValidator(timeout))
    return validators
