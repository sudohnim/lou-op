"""ModalRuntime: run model-influenced commands in a Modal Sandbox.

The first Runtime with no shared filesystem — it exercises the sync hooks:
the host repo stays the source of truth; ``sync_in`` tars it into the
sandbox before each bash/validator run, ``sync_out`` pulls the sandbox tree
back. Network is blocked by default (same default-deny as docker).

Requires ``pip install modal`` + ``modal token new``. Selected with
``--runtime modal`` (registered lazily in get_runtime).
"""

from __future__ import annotations

import io
import tarfile
from pathlib import Path
from typing import Optional

from .exec import CmdResult
from .runtime import Runtime

_EXCLUDE = (".git", ".lou-op", "__pycache__", ".venv")
_WORK = "/work"


def _tar_repo(repo_path: Path) -> bytes:
    buf = io.BytesIO()

    def keep(info: tarfile.TarInfo) -> Optional[tarfile.TarInfo]:
        parts = Path(info.name).parts
        return None if any(p in _EXCLUDE for p in parts) else info

    with tarfile.open(fileobj=buf, mode="w") as tar:
        tar.add(repo_path, arcname=".", filter=keep)
    return buf.getvalue()


def _untar_into(repo_path: Path, data: bytes) -> None:
    with tarfile.open(fileobj=io.BytesIO(data)) as tar:
        tar.extractall(repo_path, filter="data")  # refuses path escapes


class ModalRuntime(Runtime):
    def __init__(
        self,
        image_tag: str = "python:3.12-slim",
        *,
        network: bool = False,
        app_name: str = "lou-op",
        timeout_s: int = 3600,
    ) -> None:
        try:
            import modal
        except ImportError as exc:  # pragma: no cover - exercised via mock
            raise ImportError(
                "ModalRuntime needs the modal SDK: pip install modal"
                " && modal token new"
            ) from exc
        self._modal = modal
        self.image_tag = image_tag
        self.network = network
        self.app_name = app_name
        self.timeout_s = timeout_s
        self._sandbox = None

    def setup(self, job_id: str, repo_path: Path) -> None:
        modal = self._modal
        app = modal.App.lookup(self.app_name, create_if_missing=True)
        image = modal.Image.from_registry(self.image_tag)
        self._sandbox = modal.Sandbox.create(
            app=app,
            image=image,
            timeout=self.timeout_s,
            block_network=not self.network,  # default-deny egress
        )
        self._exec("mkdir", "-p", _WORK)
        self.sync_in(repo_path)

    def _exec(self, *argv: str, timeout: int = 300) -> CmdResult:
        if self._sandbox is None:
            raise RuntimeError("ModalRuntime used before setup()")
        proc = self._sandbox.exec(*argv, timeout=timeout)
        proc.wait()
        return CmdResult(proc.returncode, proc.stdout.read(), proc.stderr.read(), False)

    def shell(self, command: str, cwd: Path, *, timeout: int = 300) -> CmdResult:
        return self._exec("sh", "-c", f"cd {_WORK} && {command}", timeout=timeout)

    def sync_in(self, repo_path: Path) -> None:
        data = _tar_repo(repo_path)
        with self._sandbox.open("/tmp/in.tar", "wb") as f:
            f.write(data)
        self._exec("sh", "-c", f"cd {_WORK} && tar -xf /tmp/in.tar")

    def sync_out(self, repo_path: Path) -> None:
        self._exec("sh", "-c", f"cd {_WORK} && tar -cf /tmp/out.tar .")
        with self._sandbox.open("/tmp/out.tar", "rb") as f:
            data = f.read()
        _untar_into(repo_path, data)

    def teardown(self) -> None:
        if self._sandbox is not None:
            self._sandbox.terminate()
            self._sandbox = None
