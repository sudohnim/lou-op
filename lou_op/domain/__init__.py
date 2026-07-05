"""The pure domain: state machines and policies, zero I/O (invariant I6).

Modules here import only the standard library, ``lou_op.domain.*``, and the
port *interfaces* in ``lou_op.ports`` — never adapters, backends, config, or
frameworks. Enforced by ``tests/test_import_boundaries.py``.
"""

from .graph import TaskGraph
from .iteration import (
    Decision,
    GuardReport,
    IterationMachine,
    IterationState,
)
from .scope import Scope
from .verification import (
    Criterion,
    JudgeSignal,
    Provenance,
    VacuousSpecError,
    Verdict,
    Verification,
)

__all__ = [
    "Criterion",
    "Decision",
    "GuardReport",
    "IterationMachine",
    "IterationState",
    "JudgeSignal",
    "Provenance",
    "Scope",
    "TaskGraph",
    "VacuousSpecError",
    "Verdict",
    "Verification",
]
