"""Scope: the write-boundary policy object (P2). Fails CLOSED.

Decision logic is pure; the mechanism (revert) is delegated to the
Workspace port passed to ``enforce``.
"""

from __future__ import annotations

import fnmatch
import re
from dataclasses import dataclass, field
from typing import Callable, List, Optional

from ..ports.workspace import Workspace

_FILENAME_RE = re.compile(r"[\w./-]*\w\.\w+")


class EmptyScopeError(Exception):
    """strict mode with nothing inferable: refuse to run rather than run
    unbounded (fail closed)."""


@dataclass
class Scope:
    allowed: List[str] = field(default_factory=list)
    protected: List[str] = field(default_factory=list)
    #: paths exempt from enforcement (loop bookkeeping)
    exempt_prefixes: tuple = (".lou-op/",)

    @classmethod
    def from_task(
        cls,
        allowed: List[str],
        protected: List[str],
        *,
        strict: bool = False,
        description: str = "",
    ) -> "Scope":
        """Build the effective scope. strict + empty allowed → infer from
        the description; nothing inferable → EmptyScopeError (fail closed,
        never unlimited by accident)."""
        effective = list(allowed)
        if strict and not effective:
            effective = _FILENAME_RE.findall(description)
            if not effective:
                raise EmptyScopeError(
                    "strict scope: no allowed_paths and no files named in"
                    " the task description"
                )
        return cls(allowed=effective, protected=list(protected))

    # -- pure decision -------------------------------------------------------

    def permits(self, rel: str) -> bool:
        if any(rel.startswith(p) for p in self.exempt_prefixes):
            return True
        if not self.allowed:
            return True  # no declared scope (non-strict): everything allowed
        for pattern in self.allowed:
            if fnmatch.fnmatch(rel, pattern):
                return True
            # "dir/**" — fnmatch has no recursive glob, treat as prefix match
            if pattern.endswith("/**") and rel.startswith(
                pattern[:-3].rstrip("/") + "/"
            ):
                return True
        return False

    # -- mechanism via the port ----------------------------------------------

    def enforce(
        self,
        tree: Workspace,
        emit: Optional[Callable[[str], None]] = None,
    ) -> List[str]:
        """Revert every out-of-scope change on ``tree``; return reverted
        paths. Renames contribute both sides (drop new, restore old)."""
        if not self.allowed:
            return []
        say = emit or (lambda _: None)
        reverted: List[str] = []
        for change in tree.changed_paths():
            for rel in filter(None, (change.path, change.old_path)):
                if self.permits(rel):
                    continue
                say(f"[guard] reverting out-of-scope change: {rel}")
                tree.restore_paths([rel])
                reverted.append(rel)
        return reverted
