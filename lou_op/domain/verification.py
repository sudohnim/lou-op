"""Verification: the gate as a first-class domain object (P6, I4).

The product's soul is "the model cannot grade its own homework." That rule
is *typed* here, not scattered:

- ``provenance`` says who authored the spec. An IMPLEMENTER-authored gate
  cannot be authoritative — enforced at construction, not by checklist.
- ``frozen`` specs are immutable to the agent (mechanically enforced by the
  Scope/protected guard; recorded here as part of the gate's identity).
- Anti-vacuous is a typed precondition: ``assert_can_fail`` rejects a gate
  that passes against the current (empty) implementation — a spec that
  cannot fail is invalid, so it can never be frozen-but-useless.
- The judge is a *typed advisory signal* (``JudgeSignal``): it can veto by
  raising upstream, but its output can never flip a Verdict to passed.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import List, Optional

from ..ports.workspace import Workspace


class Provenance(Enum):
    IMPLEMENTER = "implementer"  # same model that writes the code
    INDEPENDENT = "independent"  # separate spec-author model
    HUMAN = "human"  # human-written or human-reviewed


class VacuousSpecError(Exception):
    """The gate passes with no implementation — it cannot fail, so it
    verifies nothing."""


@dataclass(frozen=True)
class Criterion:
    """One typed check. ``kind``: "test" | "lint" | "custom"."""

    kind: str
    command: str
    name: str = ""

    def label(self) -> str:
        return self.name or f"{self.kind}: {self.command[:60]}"


@dataclass
class CriterionResult:
    criterion: Criterion
    passed: bool
    output: str = ""


@dataclass
class Verdict:
    passed: bool
    results: List[CriterionResult] = field(default_factory=list)


@dataclass
class JudgeSignal:
    """Advisory-only. Consumers may log it or abort on it, but a Verdict is
    computed exclusively from criteria — this type is deliberately not an
    input to ``evaluate``."""

    consistent: bool
    notes: str = ""


class Verification:
    def __init__(
        self,
        criteria: List[Criterion],
        *,
        provenance: Provenance,
        frozen: bool = False,
        authoritative: bool = True,
        spec_paths: Optional[List[str]] = None,
    ) -> None:
        if authoritative and not criteria:
            raise ValueError(
                "an authoritative Verification needs at least one criterion"
            )
        if authoritative and provenance == Provenance.IMPLEMENTER:
            # I4, structurally: the implementer cannot author its own gate
            raise ValueError(
                "a gate authored by the implementer cannot be authoritative"
                " — use an independent spec author or human review"
            )
        self.criteria = list(criteria)
        self.provenance = provenance
        self.frozen = frozen
        self.authoritative = authoritative
        self.spec_paths = list(spec_paths or [])

    # -- the gate ------------------------------------------------------------

    def evaluate(self, tree: Workspace, *, timeout: int = 300) -> Verdict:
        results = [
            CriterionResult(
                criterion=c,
                passed=(res := tree.exec(c.command, timeout=timeout)).passed,
                output=(res.stdout + res.stderr)[-2000:],
            )
            for c in self.criteria
        ]
        return Verdict(
            passed=bool(results) and all(r.passed for r in results),
            results=results,
        )

    # -- typed precondition (anti-vacuous) ------------------------------------

    def assert_can_fail(self, tree: Workspace, *, timeout: int = 300) -> None:
        """Reject a gate that is already green before any work: it cannot
        fail, so it verifies nothing (checked at plan time, before iter 1)."""
        if not self.authoritative:
            return
        if self.evaluate(tree, timeout=timeout).passed:
            raise VacuousSpecError(
                "verification passes before any implementation exists —"
                " the spec is vacuous; strengthen it before freezing"
            )
