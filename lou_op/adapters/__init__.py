"""Adapters: real implementations of the ports. Domain never imports these."""

from .workspace_host import HostWorkspace

__all__ = ["HostWorkspace"]
