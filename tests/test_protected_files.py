"""Protected-file guard: the loop restores tampered spec files before validating."""

from __future__ import annotations

from pathlib import Path

from structlog.testing import capture_logs

from lou_op.backends.base import Backend
from lou_op.loop import run_task
from lou_op.models import IterationContext, IterationOutput, Task, ValidationResult

SPEC = "def test_truth():\n    assert True\n"


def _events(caplogs) -> str:
    """Flatten captured structlog events + their fields into one string."""
    return " ".join(
        str(e.get("event", "")) + " " + " ".join(f"{k}={v}" for k, v in e.items())
        for e in caplogs
    )


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
    """Red until impl.py exists; green only if the spec also survived intact.

    Requiring impl.py keeps this red at pre-flight — a green-before-work
    validator now trips the vacuous-spec guard and skips the model.
    """

    name = "spec-intact"

    def run(self, repo_path: Path) -> ValidationResult:
        spec_file = repo_path / "tests_spec.py"
        content = spec_file.read_text(encoding="utf-8") if spec_file.exists() else ""
        passed = content == SPEC and (repo_path / "impl.py").exists()
        return ValidationResult(name=self.name, passed=passed, output=content)


def test_tampered_protected_file_is_restored_before_validation(repo: Path) -> None:
    (repo / "tests_spec.py").write_text(SPEC, encoding="utf-8")
    task = Task(
        name="guarded",
        protected_files=["tests_spec.py"],
        max_iterations=2,
    )
    with capture_logs() as caplogs:
        results = run_task(
            repo,
            task,
            _TamperingBackend(),
            validators=[_SpecIntactValidator()],
        )
    # validator saw the restored spec, so the iteration passes
    assert results[-1].passed is True
    assert (repo / "tests_spec.py").read_text(encoding="utf-8") == SPEC
    assert "restoring protected file" in _events(caplogs)


def test_deleted_protected_file_is_recreated(repo: Path) -> None:
    (repo / "tests_spec.py").write_text(SPEC, encoding="utf-8")

    class _Deleter(_TamperingBackend):
        def run_iteration(self, ctx: IterationContext) -> IterationOutput:
            (ctx.repo_path / "tests_spec.py").unlink()
            return IterationOutput(done=True, summary="Wrote: nothing", log="")

    task = Task(name="g", protected_files=["tests_spec.py"], max_iterations=1)
    run_task(repo, task, _Deleter(), validators=[_SpecIntactValidator()])
    assert (repo / "tests_spec.py").read_text(encoding="utf-8") == SPEC


class _AlwaysPassValidator:
    name = "always-pass"

    def run(self, repo_path: Path) -> ValidationResult:
        return ValidationResult(name=self.name, passed=True, output="")


class _ExplodingBackend(Backend):
    """Fails the test if the loop calls the model at all."""

    name = "exploding"
    include_code = False
    raw_api = False

    def run_iteration(self, ctx: IterationContext) -> IterationOutput:
        raise AssertionError("model must not be called when spec is vacuous")


def test_vacuous_spec_skips_model(repo: Path) -> None:
    """Validators green before any work → guard fires, zero model calls."""
    with capture_logs() as caplogs:
        results = run_task(
            repo,
            Task(name="vacuous", max_iterations=3),
            _ExplodingBackend(),
            validators=[_AlwaysPassValidator()],
        )
    assert len(results) == 1
    assert results[0].iteration == 0
    assert results[0].passed is True
    assert "vacuous" in _events(caplogs)
