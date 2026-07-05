"""Ports: the domain's only view of the outside world.

Each port is an abstract interface with real adapters in ``lou_op.adapters``
and deterministic fakes in tests. Domain code imports ports, never adapters
(invariant I6).
"""

from .workspace import Changed, ExecResult, Workspace, WorkspaceError

__all__ = ["Changed", "ExecResult", "Workspace", "WorkspaceError"]
