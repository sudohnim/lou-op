from __future__ import annotations

from pathlib import Path

from lou_op.backends.mock import MockBackend
from lou_op.git_ops import log
from lou_op.loop import run_task
from lou_op.models import FileWrite, IterationContext, IterationOutput, Task


def test_mock_loop_completes_and_commits(repo: Path):
    task = Task(
        name="Calculator add()",
        description="implement add",
        success_criteria=["python -m pytest -q"],
        max_iterations=3,
    )
    results = run_task(repo, task, MockBackend(), budget=10_000)

    assert results[-1].done
    assert results[-1].passed
    assert (repo / "calc.py").exists()
    # one commit per iteration; mock finishes in a single iteration.
    assert len(results) == 1
    commits = log(repo, 10)
    assert any("iteration 1" in c for c in commits)


def test_progress_file_written(repo: Path):
    # criterion must be red before work — a trivially-green one (e.g. "true")
    # now trips the vacuous-spec guard and skips the model entirely
    task = Task(name="Calculator add()", success_criteria=["python -m pytest -q"])
    run_task(repo, task, MockBackend(), budget=10_000)
    progress = (repo / ".lou-op" / "progress.md").read_text()
    assert progress.strip()  # scratchpad written (content from backend or fallback)


class _FailingThenDoneBackend(MockBackend):
    """Fails validators on iter 1, then writes the passing project."""

    include_code = True
    raw_api = False

    def __init__(self) -> None:
        super().__init__()
        self.calls = 0

    def run_iteration(self, ctx: IterationContext) -> IterationOutput:
        self.calls += 1
        if self.calls == 1:
            from lou_op.protocol import write_files

            write_files(ctx.repo_path, [FileWrite("calc.py", "broken")])
            return IterationOutput(done=False, summary="broken", log="")
        return super().run_iteration(ctx)


def test_loop_iterates_until_pass(repo: Path):
    task = Task(
        name="Calculator add()",
        success_criteria=["python -m pytest -q"],
        max_iterations=4,
    )
    results = run_task(repo, task, _FailingThenDoneBackend(), budget=10_000)
    assert len(results) == 2
    assert not results[0].passed
    assert results[1].passed


def test_clean_checkout_gate_catches_gitignored_source(repo: Path):
    """Source that .gitignore silently drops passes on the dirty working tree
    but must FAIL the clean-checkout gate — the committed branch wouldn't
    build. This is the exact kuma-kare `lib/`-ignored-by-a-Python-gitignore
    bug."""

    class _IgnoredLibBackend(MockBackend):
        def run_iteration(self, ctx: IterationContext) -> IterationOutput:
            from lou_op.protocol import write_files

            write_files(
                ctx.repo_path,
                [
                    FileWrite(".gitignore", "lib/\n"),
                    FileWrite("app.py", "import lib.helper\n\nvalue = lib.helper.v\n"),
                    FileWrite("lib/__init__.py", ""),
                    FileWrite("lib/helper.py", "v = 1\n"),
                ],
            )
            return IterationOutput(done=True, summary="wrote app", log="")

    task = Task(
        name="ignored-lib",
        success_criteria=['python -c "import app"'],
        max_iterations=1,
    )
    results = run_task(repo, task, _IgnoredLibBackend(), budget=10_000)
    # dirty tree has lib/ so `import app` works, but lib/ is git-ignored → the
    # clean checkout has no lib/ → import fails → the gate must not pass
    assert not results[-1].passed
    # the failure must name the git-ignored path so the model can self-fix
    joined = "\n".join(v.output for v in results[-1].validations)
    assert "GIT-IGNORED" in joined and "lib" in joined


def test_clean_build_artifacts_removes_untracked_keeps_tracked(tmp_path: Path):
    """Backstop against stale-artifact false passes: an untracked build dir is
    wiped before a gate runs, but a committed ``build/`` source tree is left
    alone."""
    import subprocess

    from lou_op.loop import _clean_build_artifacts

    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    (tmp_path / "dist").mkdir()
    (tmp_path / "dist" / "stale.js").write_text("old", encoding="utf-8")
    (tmp_path / "build").mkdir()
    (tmp_path / "build" / "src.py").write_text("real", encoding="utf-8")
    subprocess.run(["git", "-C", str(tmp_path), "add", "build/src.py"], check=True)

    _clean_build_artifacts(tmp_path)

    assert not (tmp_path / "dist").exists()  # untracked artifact → removed
    assert (tmp_path / "build" / "src.py").exists()  # tracked source → kept
