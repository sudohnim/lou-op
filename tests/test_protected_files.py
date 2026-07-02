"""Protected-file guard: the loop restores tampered spec files before validating."""

from __future__ import annotations

from pathlib import Path

from lou_op.backends.base import Backend
from lou_op.loop import run_task
from lou_op.models import IterationContext, IterationOutput, Task, ValidationResult

SPEC = "def test_truth():\n    assert True\n"


class _TamperingBackend(Backend):
    """Simulates a model that rewrites the protected test file to pass."""

    name = "tamper"
    include_code = False
    raw_api = False

    def run_iteration(self, ctx: IterationContext) -> IterationOutput:
        (ctx.repo_path / "tests_spec.py").write_text("# gamed\n", encoding="utf-8")
        (ctx.repo_path / "impl.py").write_text("x = 1\n", encoding="utf-8")
        return IterationOutput(done=True, summary="Wrote: impl.py", log="")


class _SpecIntactValidator:
    """Passes only if the protected file still holds the original spec."""

    name = "spec-intact"

    def run(self, repo_path: Path) -> ValidationResult:
        content = (repo_path / "tests_spec.py").read_text(encoding="utf-8")
        return ValidationResult(name=self.name, passed=content == SPEC, output=content)


def test_tampered_protected_file_is_restored_before_validation(repo: Path) -> None:
    (repo / "tests_spec.py").write_text(SPEC, encoding="utf-8")
    task = Task(
        name="guarded",
        protected_files=["tests_spec.py"],
        max_iterations=2,
    )
    lines: list[str] = []
    results = run_task(
        repo,
        task,
        _TamperingBackend(),
        validators=[_SpecIntactValidator()],
        on_line=lines.append,
    )
    # validator saw the restored spec, so the iteration passes
    assert results[-1].passed is True
    assert (repo / "tests_spec.py").read_text(encoding="utf-8") == SPEC
    assert any("restoring protected file" in line for line in lines)


def test_deleted_protected_file_is_recreated(repo: Path) -> None:
    (repo / "tests_spec.py").write_text(SPEC, encoding="utf-8")

    class _Deleter(_TamperingBackend):
        def run_iteration(self, ctx: IterationContext) -> IterationOutput:
            (ctx.repo_path / "tests_spec.py").unlink()
            return IterationOutput(done=True, summary="Wrote: nothing", log="")

    task = Task(name="g", protected_files=["tests_spec.py"], max_iterations=1)
    run_task(repo, task, _Deleter(), validators=[_SpecIntactValidator()])
    assert (repo / "tests_spec.py").read_text(encoding="utf-8") == SPEC
