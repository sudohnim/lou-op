"""Execution runtimes: where model-influenced commands actually run.

``host`` (default) is the existing behavior — subprocesses on the host with a
scrubbed environment. ``docker`` runs everything in a locked-down per-job
container: --cap-drop ALL, no-new-privileges, non-root, repo bind-mounted at
/work, optionally no network. Select with --runtime / LOU_RUNTIME.
"""

from __future__ import annotations

import os
import subprocess
from abc import ABC, abstractmethod
from pathlib import Path
from typing import List, Optional

from .exec import CmdResult, run_shell

_DEFAULT_IMAGE = "python:3.12-slim"


class Runtime(ABC):
    """One job's command executor. setup → (sync_in → shell → sync_out)* → teardown.

    File-transfer hooks exist for runtimes without a shared filesystem
    (cloud sandboxes like Modal). Host and docker share the repo directly
    (bind mount), so their sync hooks are no-ops.
    """

    @abstractmethod
    def setup(self, job_id: str, repo_path: Path) -> None: ...

    @abstractmethod
    def shell(self, command: str, cwd: Path, *, timeout: int = 300) -> CmdResult: ...

    @abstractmethod
    def teardown(self) -> None: ...

    def sync_in(self, repo_path: Path) -> None:
        """Push the local repo state to the execution environment."""

    def sync_out(self, repo_path: Path) -> None:
        """Pull files the execution environment created back to the repo."""


class HostRuntime(Runtime):
    """Byte-for-byte the pre-runtime behavior: exec.run_shell on the host."""

    def setup(self, job_id: str, repo_path: Path) -> None:
        pass

    def shell(self, command: str, cwd: Path, *, timeout: int = 300) -> CmdResult:
        return run_shell(command, cwd, timeout=timeout)

    def teardown(self) -> None:
        pass


class DockerRuntime(Runtime):
    """Per-job hardened container; commands run via ``docker exec``."""

    def __init__(
        self,
        image: str = _DEFAULT_IMAGE,
        *,
        network: bool = False,
        user: Optional[str] = None,
    ) -> None:
        self.image = image
        self.network = network
        # non-root inside the container; default to the host uid:gid so the
        # bind-mounted repo stays owned by the invoking user
        self.user = user or f"{os.getuid()}:{os.getgid()}"
        self._job_id: Optional[str] = None

    @staticmethod
    def container_name(job_id: str) -> str:
        return f"lou-op-{job_id}"

    def create_argv(self, job_id: str, repo_path: Path) -> List[str]:
        """The hardened ``docker run`` command (pure — unit-testable)."""
        argv = [
            "docker",
            "run",
            "-d",
            "--rm",
            "--name",
            self.container_name(job_id),
            "--cap-drop",
            "ALL",
            "--security-opt",
            "no-new-privileges",
            "--user",
            self.user,
            "-v",
            f"{repo_path.resolve()}:/work",
            "-w",
            "/work",
        ]
        if not self.network:
            argv += ["--network", "none"]
        argv += [self.image, "sleep", "infinity"]
        return argv

    def exec_argv(self, job_id: str, command: str) -> List[str]:
        return [
            "docker",
            "exec",
            "-w",
            "/work",
            self.container_name(job_id),
            "sh",
            "-c",
            command,
        ]

    def setup(self, job_id: str, repo_path: Path) -> None:
        self._job_id = job_id
        result = subprocess.run(
            self.create_argv(job_id, repo_path),
            capture_output=True,
            text=True,
            timeout=120,
        )
        if result.returncode != 0:
            raise RuntimeError(f"docker runtime setup failed: {result.stderr.strip()}")

    def shell(self, command: str, cwd: Path, *, timeout: int = 300) -> CmdResult:
        if self._job_id is None:
            raise RuntimeError("DockerRuntime.shell before setup()")
        try:
            proc = subprocess.run(
                self.exec_argv(self._job_id, command),
                capture_output=True,
                text=True,
                timeout=timeout,
            )
        except subprocess.TimeoutExpired as exc:
            out = exc.stdout.decode() if isinstance(exc.stdout, bytes) else ""
            return CmdResult(-1, out, "timed out", True)
        return CmdResult(proc.returncode, proc.stdout, proc.stderr, False)

    def teardown(self) -> None:
        if self._job_id is None:
            return
        subprocess.run(
            ["docker", "rm", "-f", self.container_name(self._job_id)],
            capture_output=True,
            timeout=60,
        )
        self._job_id = None


# Third-party runtimes register here (directly, or via the
# "lou_op.runtimes" entry-point group — e.g. a ModalRuntime package).
_RUNTIME_REGISTRY: dict = {}


def register_runtime(name: str, factory) -> None:
    """Register a runtime factory: ``factory(network=bool) -> Runtime``."""
    _RUNTIME_REGISTRY[name.strip().lower()] = factory


def _load_entry_point(key: str):
    from importlib.metadata import entry_points

    for ep in entry_points(group="lou_op.runtimes"):
        if ep.name == key:
            return ep.load()
    return None


def get_runtime(name: str, *, network: bool = False) -> Runtime:
    key = (name or "host").strip().lower()
    if key == "host":
        return HostRuntime()
    if key == "docker":
        return DockerRuntime(network=network)
    if key == "modal":
        from .modal_runtime import ModalRuntime  # lazy: needs modal SDK

        return ModalRuntime(network=network)
    factory = _RUNTIME_REGISTRY.get(key)
    if factory is None:
        factory = _load_entry_point(key)
        if factory is not None:
            _RUNTIME_REGISTRY[key] = factory
    if factory is not None:
        return factory(network=network)
    known = ["host", "docker", *sorted(_RUNTIME_REGISTRY)]
    raise ValueError(f"unknown runtime: {name!r} ({' | '.join(known)})")
