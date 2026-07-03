"""Spec (P1.2): scope revert must survive real git status output.

The old parser read text porcelain — git quotes filenames with spaces and
emits ``R old -> new`` for renames, both of which silently broke the revert.
Parse ``git status -z --porcelain`` (NUL-delimited, unquoted) and handle
renames as restore-old + remove-new. Plus: ``strict_scope`` infers the
allowed set from the task description when ``allowed_paths`` is empty.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

from lou_op.backends.base import Backend
from lou_op.loop import run_task
from lou_op.models import IterationContext, IterationOutput, Task, ValidationResult


def _git(repo: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=repo, capture_output=True, check=False)


def _seed_commit(repo: Path, name: str, content: str = "seed\n") -> None:
    (repo / name).write_text(content)
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", f"seed {name}")


class _PassValidator:
    name = "ok"

    def run(self, repo_path: Path) -> ValidationResult:
        return ValidationResult(name=self.name, passed=True, output="")


class _RedThenGreen:
    """Red at pre-flight (impl.py absent), green after the backend runs."""

    name = "impl-exists"

    def run(self, repo_path: Path) -> ValidationResult:
        return ValidationResult(
            name=self.name, passed=(repo_path / "impl.py").exists(), output=""
        )


def _backend(effect) -> Backend:
    class _B(Backend):
        name = "scripted"
        include_code = False
        raw_api = False

        def run_iteration(self, ctx: IterationContext) -> IterationOutput:
            effect(ctx.repo_path)
            (ctx.repo_path / "impl.py").write_text("x = 1\n")
            return IterationOutput(done=True, summary="Wrote: impl.py", log="")

    return _B()


def _run(repo: Path, backend: Backend, task: Task, **kwargs):
    return run_task(repo, task, backend, validators=[_RedThenGreen()], **kwargs)


def test_untracked_file_with_spaces_reverted(repo: Path) -> None:
    """Quoted-porcelain trap: 'my file.py' must be found and deleted."""
    task = Task(
        name="t",
        allowed_paths=["impl.py"],
        success_criteria=["true"],
        max_iterations=1,
    )
    _run(repo, _backend(lambda r: (r / "my file.py").write_text("junk")), task)
    assert not (repo / "my file.py").exists()
    assert (repo / "impl.py").exists()


def test_rename_reverted_both_sides(repo: Path) -> None:
    """git mv a.txt b.txt out of scope → a.txt restored, b.txt gone."""
    _seed_commit(repo, "a.txt", "original\n")

    def mv(r: Path) -> None:
        subprocess.run(["git", "mv", "a.txt", "b.txt"], cwd=r, capture_output=True)

    task = Task(
        name="t",
        allowed_paths=["impl.py"],
        success_criteria=["true"],
        max_iterations=1,
    )
    _run(repo, _backend(mv), task)
    assert (repo / "a.txt").read_text() == "original\n"
    assert not (repo / "b.txt").exists()


def test_modified_tracked_file_with_spaces_restored(repo: Path) -> None:
    _seed_commit(repo, "spaced name.txt", "original\n")
    task = Task(
        name="t",
        allowed_paths=["impl.py"],
        success_criteria=["true"],
        max_iterations=1,
    )
    _run(
        repo,
        _backend(lambda r: (r / "spaced name.txt").write_text("vandalized")),
        task,
    )
    assert (repo / "spaced name.txt").read_text() == "original\n"


def test_strict_scope_infers_from_description(repo: Path) -> None:
    """allowed_paths empty + strict → only files named in the description live."""
    task = Task(
        name="t",
        description="Implement impl.py to satisfy the spec.",
        success_criteria=["true"],
        max_iterations=1,
    )
    _run(
        repo,
        _backend(lambda r: (r / "sneaky.py").write_text("junk")),
        task,
        strict_scope=True,
    )
    assert (repo / "impl.py").exists()  # named in description → kept
    assert not (repo / "sneaky.py").exists()  # unnamed → reverted


def test_strict_scope_fails_closed_when_nothing_inferable(repo: Path) -> None:
    """strict + no allowed_paths + description naming no files → the task
    FAILS before the backend runs; it must never fall back to unlimited."""
    ran: list[bool] = []
    task = Task(
        name="t",
        description="Make everything better.",  # no filenames to infer
        success_criteria=["true"],
        max_iterations=1,
    )
    results = _run(
        repo,
        _backend(lambda r: ran.append(True)),
        task,
        strict_scope=True,
    )
    assert not ran  # backend never invoked
    assert not results[-1].passed and not results[-1].done


def test_default_nonstrict_allows_everything(repo: Path) -> None:
    """Back-compat: empty allowed_paths without strict reverts nothing."""
    task = Task(name="t", success_criteria=["true"], max_iterations=1)
    _run(repo, _backend(lambda r: (r / "extra.py").write_text("fine")), task)
    assert (repo / "extra.py").exists()
