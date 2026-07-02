"""Seeded spec: progress.md hygiene (US-103), Chief's "Codebase Patterns" idea.

Implement ``trim_progress`` in lou_op/progress.py and call it in the loop
before writing progress.md — fresh-context iterations need curated memory,
not an unbounded append-only log.
"""

from __future__ import annotations

from lou_op.progress import trim_progress

PATTERNS = """\
## Codebase Patterns
- validators gate done; model claims are advisory
- tests live in tests/, one file per module
"""


def _entry(n: int) -> str:
    return f"\n## Iteration {n} — tests failing\n**Files:** store.py\n"


def test_keeps_patterns_and_last_n_entries() -> None:
    text = PATTERNS + "".join(_entry(i) for i in range(1, 11))
    out = trim_progress(text, max_entries=3)
    assert "## Codebase Patterns" in out
    assert "validators gate done" in out
    assert "## Iteration 10" in out and "## Iteration 8" in out
    assert "## Iteration 7" not in out and "## Iteration 1" not in out


def test_no_patterns_section_still_trims() -> None:
    text = "".join(_entry(i) for i in range(1, 6))
    out = trim_progress(text, max_entries=2)
    assert "## Iteration 5" in out and "## Iteration 4" in out
    assert "## Iteration 3" not in out


def test_under_limit_unchanged() -> None:
    text = PATTERNS + _entry(1)
    assert trim_progress(text, max_entries=5).strip() == text.strip()
