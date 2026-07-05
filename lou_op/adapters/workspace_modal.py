"""ModalWorkspace: host tree is the source of truth; exec transparently
ships it to a Modal Sandbox, runs there, and pulls results back.

The transfer is INTERNAL to ``exec`` — no public sync_in/sync_out for the
domain to sequence (the v3-A1 bug was exactly a caller forgetting that
sequencing). Every exec sees the current host tree by construction:
guard-restored files included, because the push happens after guards ran,
inside the same call that validates (I1/I2).

File/VCS ops are inherited from HostWorkspace (host tree). Deadline is
enforced sandbox-side via ``timeout -s KILL`` (I8).
"""

from __future__ import annotations

import shlex
import time
from pathlib import Path
from typing import Optional

from ..modal_runtime import ModalRuntime
from ..ports.workspace import ExecResult
from .workspace_host import HostWorkspace


class ModalWorkspace(HostWorkspace):
    def __init__(
        self,
        root: Path,
        *,
        network: bool = False,
        image_tag: Optional[str] = None,
    ) -> None:
        super().__init__(root)
        kwargs = {"network": network}
        if image_tag:
            kwargs["image_tag"] = image_tag
        self._runtime = ModalRuntime(**kwargs)

    def setup(self, job_id: str) -> None:
        self._runtime.setup(job_id, self.root)

    def teardown(self) -> None:
        self._runtime.teardown()

    def exec(
        self,
        command: str,
        *,
        timeout: int = 300,
        deadline: Optional[float] = None,
    ) -> ExecResult:
        budget = float(timeout)
        if deadline is not None:
            budget = max(1.0, min(budget, deadline - time.monotonic()))
        # transfer is internal: push current host tree, run, pull results
        self._runtime.sync_in(self.root)
        wrapped = f"timeout -s KILL {int(budget)} sh -c {shlex.quote(command)}"
        res = self._runtime.shell(wrapped, self.root, timeout=int(budget) + 30)
        self._runtime.sync_out(self.root)
        killed = res.returncode in (124, 137)
        return ExecResult(
            res.returncode,
            res.stdout,
            res.stderr,
            timed_out=res.timed_out or killed,
            killed=killed,
        )
