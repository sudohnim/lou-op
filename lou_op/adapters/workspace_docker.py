"""DockerWorkspace: same host tree (bind-mounted at /work), exec inside a
hardened container.

File/VCS ops are inherited from HostWorkspace — the bind mount means there
is literally one tree, so guards, the agent's writes, and validators can
never diverge (I1). Only ``exec`` differs: it runs in the container, wrapped
in ``timeout -s KILL`` so a deadline breach is enforced *inside* the
container (I8) — killing the docker-exec client alone would leave the
command running.
"""

from __future__ import annotations

import shlex
import time
from pathlib import Path
from typing import Optional

from ..ports.workspace import ExecResult
from ..runtime import DockerRuntime
from .workspace_host import HostWorkspace


class DockerWorkspace(HostWorkspace):
    def __init__(
        self,
        root: Path,
        *,
        network: bool = False,
        image: Optional[str] = None,
    ) -> None:
        super().__init__(root)
        kwargs = {"network": network}
        if image:
            kwargs["image"] = image
        self._runtime = DockerRuntime(**kwargs)

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
        # container-side hard kill: timeout(1) sends SIGKILL at the budget
        wrapped = f"timeout -s KILL {int(budget)} sh -c {shlex.quote(command)}"
        res = self._runtime.shell(wrapped, self.root, timeout=int(budget) + 10)
        killed = res.returncode in (124, 137)  # timeout(1) / SIGKILL exits
        return ExecResult(
            res.returncode,
            res.stdout,
            res.stderr,
            timed_out=res.timed_out or killed,
            killed=killed,
        )
