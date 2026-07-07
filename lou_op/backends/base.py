"""The single backend interface every engine implements."""

from __future__ import annotations

from abc import ABC, abstractmethod

from ..models import IterationContext, IterationOutput


class Backend(ABC):
    """Drives one iteration. Side effect: files land in ``ctx.repo_path``.

    Implementations may write files themselves (raw-API/mock) or delegate to a
    coding-agent CLI that writes them (agent-CLI). Either way the loop runs
    validators and owns the commit afterward.
    """

    name: str

    #: Whether the loop should serialize the codebase into the prompt. False
    #: for agent-CLI (the agent reads the repo itself).
    include_code: bool = True

    #: Whether the prompt should include the raw-API file protocol.
    raw_api: bool = False

    @abstractmethod
    def run_iteration(self, ctx: IterationContext) -> IterationOutput:
        ...

    def use_workspace(self, tree) -> None:
        """Adopt the job's Workspace (the ONE working tree, I1).

        Default is a no-op; backends that touch the world (native)
        override this so every model-authored read/write/exec goes through
        the same tree the guards and validators use."""
