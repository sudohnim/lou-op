"""The Iteration state machine — pure, preemptible, no side effects (P2, I8).

``GENERATE → GUARD → VALIDATE → COMMIT → decide(DONE | CONTINUE | STOP)``

Given inputs (agent output, guard report, verdict) it returns the next
state; mechanisms (model calls, git, exec) happen in ports, *sequenced* by
this machine. An external deadline/cancel forces INTERRUPT from any state —
termination is never deferred to the next boundary.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import List, Optional


class IterationState(Enum):
    GENERATE = "generate"
    GUARD = "guard"
    VALIDATE = "validate"
    COMMIT = "commit"
    DONE = "done"
    CONTINUE = "continue"
    STOP = "stop"
    INTERRUPTED = "interrupted"


class Decision(Enum):
    DONE = "done"
    CONTINUE = "continue"
    STOP = "stop"


@dataclass
class AgentReport:
    """What the agent did this iteration (pure data, produced by a port)."""

    claimed_done: bool
    wrote_files: bool


@dataclass
class GuardReport:
    """What the guards did: reverted paths + restored protected files."""

    reverted: List[str] = field(default_factory=list)
    restored: List[str] = field(default_factory=list)


@dataclass
class VerdictInput:
    passed: bool


class IterationMachine:
    """Pure transition function over one iteration's lifecycle.

    Terminal states: DONE, STOP, INTERRUPTED. ``interrupt()`` is legal from
    ANY non-terminal state (I8) — the caller hard-cancels the in-flight
    mechanism and records the interruption.
    """

    _TERMINAL = {
        IterationState.DONE,
        IterationState.STOP,
        IterationState.INTERRUPTED,
    }

    def __init__(self) -> None:
        self.state = IterationState.GENERATE
        self._agent: Optional[AgentReport] = None
        self._verdict: Optional[VerdictInput] = None

    # -- transitions --------------------------------------------------------

    def generated(self, report: AgentReport) -> IterationState:
        self._require(IterationState.GENERATE)
        self._agent = report
        self.state = IterationState.GUARD
        return self.state

    def guarded(self, report: GuardReport) -> IterationState:
        self._require(IterationState.GUARD)
        self.state = IterationState.VALIDATE
        return self.state

    def validated(self, verdict: VerdictInput) -> IterationState:
        self._require(IterationState.VALIDATE)
        self._verdict = verdict
        self.state = IterationState.COMMIT
        return self.state

    def committed(self) -> IterationState:
        self._require(IterationState.COMMIT)
        self.state = self._decide()
        return self.state

    def interrupt(self) -> IterationState:
        """Deadline/cancel: legal from any non-terminal state (I8)."""
        if self.state not in self._TERMINAL:
            self.state = IterationState.INTERRUPTED
        return self.state

    # -- decision -----------------------------------------------------------

    def _decide(self) -> IterationState:
        assert self._agent is not None and self._verdict is not None
        if self._verdict.passed:
            # validators are the gate; the model's done claim is advisory
            return IterationState.DONE
        if not self._agent.wrote_files and not self._agent.claimed_done:
            # true no-op: nothing written, no claim — iterating is pointless
            return IterationState.STOP
        return IterationState.CONTINUE

    # -- helpers ------------------------------------------------------------

    def _require(self, expected: IterationState) -> None:
        if self.state != expected:
            raise ValueError(
                f"illegal transition: in {self.state.value}, expected"
                f" {expected.value}"
            )

    @property
    def is_terminal(self) -> bool:
        return self.state in self._TERMINAL
