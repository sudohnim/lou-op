"""The Store port: durable, event-sourced run state (P3, I3).

Append-only event log = single source of truth. ``RunState`` is a fold over
events; git commits, progress.md and the audit trail are projections, not
competing stores. Events carry a schema ``version`` so old logs still fold
after shape changes; ``save_snapshot`` checkpoints the fold so resume cost
is bounded by the latest snapshot, not O(history).
"""

from __future__ import annotations

import time
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

# -- events -------------------------------------------------------------------


@dataclass(frozen=True)
class Event:
    seq: int
    run_id: str
    kind: str
    version: int
    data: dict
    ts: float


# -- the folded state ----------------------------------------------------------


@dataclass
class RunState:
    run_id: str = ""
    status: str = "created"  # created | running | completed | failed | ...
    error: str = ""
    task_status: Dict[str, str] = field(default_factory=dict)
    iterations: Dict[str, int] = field(default_factory=dict)
    commits: List[str] = field(default_factory=list)
    tokens_total: int = 0
    cost_usd: float = 0.0
    last_seq: int = 0


def fold(state: RunState, event: Event) -> RunState:
    """Pure fold step. Tolerates old event versions: every branch reads
    with .get defaults, so a v1 event (fewer fields) still applies."""
    state.last_seq = event.seq
    data = event.data
    if event.kind == "run_created":
        state.run_id = event.run_id
        state.status = "created"
        for name in data.get("tasks", []):
            state.task_status.setdefault(name, "pending")
    elif event.kind == "run_started":
        state.status = "running"
    elif event.kind == "task_status":
        state.task_status[data["task"]] = data["status"]
    elif event.kind == "iteration":
        task = data.get("task", "")
        state.iterations[task] = data.get("n", state.iterations.get(task, 0) + 1)
        if data.get("commit"):
            state.commits.append(data["commit"])
        state.tokens_total += int(data.get("tokens", 0))
        state.cost_usd += float(data.get("cost_usd", 0.0))
    elif event.kind == "run_finished":
        state.status = data.get("status", "completed")
        state.error = data.get("error", "")
    # unknown kinds are skipped — forward compatibility
    return state


# -- the port -------------------------------------------------------------------


class Store(ABC):
    @abstractmethod
    def append(self, run_id: str, kind: str, data: dict, *, version: int = 1) -> Event:
        ...

    @abstractmethod
    def events(self, run_id: str, *, after_seq: int = 0) -> List[Event]:
        ...

    @abstractmethod
    def save_snapshot(self, run_id: str, state: RunState) -> None:
        ...

    @abstractmethod
    def latest_snapshot(self, run_id: str) -> Optional[RunState]:
        ...

    @abstractmethod
    def run_ids(self) -> List[str]:
        ...

    def load(self, run_id: str) -> Tuple[RunState, int]:
        """Rebuild state: latest snapshot + only the events after it.

        Returns ``(state, events_replayed)`` — the second element exists so
        tests can prove resume cost is snapshot-bounded.
        """
        state = self.latest_snapshot(run_id) or RunState(run_id=run_id)
        tail = self.events(run_id, after_seq=state.last_seq)
        for event in tail:
            fold(state, event)
        return state, len(tail)


def now() -> float:
    return time.time()
