"""TaskGraph: pure dependency scheduling over task statuses (P2/P7).

No I/O, no Task-object mutation — the graph reads a status mapping and
answers ``ready()`` / ``is_complete()`` / ``blocked()``. The scheduler in
the interfaces layer is a thin loop over these pure answers.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List, Mapping, Sequence

# statuses the graph understands (string-typed to stay framework-free)
PENDING = "pending"
IN_PROGRESS = "in_progress"
PASSED = "passed"
FAILED = "failed"


class GraphError(Exception):
    """Unknown dependency or dependency cycle."""


@dataclass(frozen=True)
class Node:
    name: str
    depends_on: Sequence[str] = field(default_factory=tuple)


class TaskGraph:
    def __init__(self, nodes: Sequence[Node]) -> None:
        self.nodes = list(nodes)
        self._by_name: Dict[str, Node] = {n.name: n for n in self.nodes}
        self._check_known_deps()
        self._check_acyclic()

    # -- construction checks ------------------------------------------------

    def _check_known_deps(self) -> None:
        for node in self.nodes:
            for dep in node.depends_on:
                if dep not in self._by_name:
                    raise GraphError(
                        f"task '{node.name}' depends on unknown task '{dep}'"
                    )

    def _check_acyclic(self) -> None:
        visited: set = set()
        stack: set = set()

        def visit(name: str) -> None:
            if name in stack:
                raise GraphError(f"dependency cycle through '{name}'")
            if name in visited:
                return
            stack.add(name)
            for dep in self._by_name[name].depends_on:
                visit(dep)
            stack.remove(name)
            visited.add(name)

        for node in self.nodes:
            visit(node.name)

    # -- pure queries over an external status map ---------------------------

    def ready(self, status: Mapping[str, str]) -> List[str]:
        """Names runnable now: PENDING/IN_PROGRESS with all deps PASSED,
        in declaration order. Resumable (IN_PROGRESS) work sorts first."""
        resumable: List[str] = []
        fresh: List[str] = []
        for node in self.nodes:
            st = status.get(node.name, PENDING)
            if st not in (PENDING, IN_PROGRESS):
                continue
            deps_passed = all(status.get(d, PENDING) == PASSED for d in node.depends_on)
            if not deps_passed:
                continue
            (resumable if st == IN_PROGRESS else fresh).append(node.name)
        return resumable + fresh

    def is_complete(self, status: Mapping[str, str]) -> bool:
        return all(status.get(n.name, PENDING) in (PASSED, FAILED) for n in self.nodes)

    def blocked(self, status: Mapping[str, str]) -> List[str]:
        """PENDING tasks that can never run: a dependency FAILED (or the
        graph has stalled). Used to fail loudly instead of hanging."""
        out: List[str] = []
        for node in self.nodes:
            if status.get(node.name, PENDING) != PENDING:
                continue
            if any(status.get(d, PENDING) == FAILED for d in node.depends_on):
                out.append(node.name)
        return out


def schedule(
    graph: TaskGraph,
    status: Mapping[str, str],
    in_flight: Sequence[str],
    *,
    max_parallel: int = 1,
    failed: bool = False,
) -> List[str]:
    """Pure scheduler step: which tasks to launch right now (P7).

    Fail-fast: after any failure nothing new starts. Bounded by
    ``max_parallel`` minus what is already running. Never re-launches
    in-flight work.
    """
    if failed:
        return []
    capacity = max(0, max(1, max_parallel) - len(in_flight))
    if capacity == 0:
        return []
    launch = [name for name in graph.ready(status) if name not in in_flight]
    return launch[:capacity]
