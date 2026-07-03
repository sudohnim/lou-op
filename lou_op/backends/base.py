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
    def run_iteration(self, ctx: IterationContext) -> IterationOutput: ...

    def use_runtime(self, runtime) -> None:
        """Adopt a Runtime for model-influenced execution (bash tool etc.).

        Default is a no-op; backends that execute commands (native)
        override this so ALL model-authored commands go through the same
        sandbox the validators use."""
