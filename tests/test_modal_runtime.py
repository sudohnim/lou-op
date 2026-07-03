"""Spec (D1): ModalRuntime against a fake modal SDK — verifies sandbox
lifecycle, default-deny network, command routing, and tar-based sync
without a Modal account. Live behavior is exercised by an actual
--runtime modal run."""

from __future__ import annotations

import io
import sys
import tarfile
import types
from pathlib import Path

import pytest


class _FakeProc:
    def __init__(self, returncode=0, stdout="ok", stderr=""):
        self.returncode = returncode
        self.stdout = io.StringIO(stdout)
        self.stderr = io.StringIO(stderr)

    def wait(self):
        pass


class _FakeFile:
    def __init__(self, store, path, mode):
        self.store, self.path, self.mode = store, path, mode
        self._buf = io.BytesIO(store.get(path, b"") if "r" in mode else b"")

    def write(self, data):
        self.store[self.path] = data

    def read(self):
        return self.store.get(self.path, b"")

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class _FakeSandbox:
    created_with: dict = {}

    def __init__(self):
        self.execs: list[tuple] = []
        self.files: dict[str, bytes] = {}
        self.terminated = False

    @classmethod
    def create(cls, **kwargs):
        cls.created_with = kwargs
        return cls._instance

    def exec(self, *argv, timeout=300):
        self.execs.append(argv)
        # emulate tar -cf out: produce a tarball containing one file
        if any("tar -cf" in a for a in argv):
            buf = io.BytesIO()
            with tarfile.open(fileobj=buf, mode="w") as tar:
                data = b"made-in-sandbox"
                info = tarfile.TarInfo("sandbox_file.txt")
                info.size = len(data)
                tar.addfile(info, io.BytesIO(data))
            self.files["/tmp/out.tar"] = buf.getvalue()
        return _FakeProc()

    def open(self, path, mode):
        return _FakeFile(self.files, path, mode)

    def terminate(self):
        self.terminated = True


@pytest.fixture()
def fake_modal(monkeypatch):
    sb = _FakeSandbox()
    _FakeSandbox._instance = sb
    mod = types.SimpleNamespace(
        App=types.SimpleNamespace(lookup=lambda name, create_if_missing: "app"),
        Image=types.SimpleNamespace(from_registry=lambda tag: f"img:{tag}"),
        Sandbox=_FakeSandbox,
    )
    monkeypatch.setitem(sys.modules, "modal", mod)
    return sb


def _runtime(**kwargs):
    from lou_op.modal_runtime import ModalRuntime

    return ModalRuntime(**kwargs)


def test_network_blocked_by_default(fake_modal, tmp_path: Path) -> None:
    rt = _runtime()
    rt.setup("job1", tmp_path)
    assert _FakeSandbox.created_with["block_network"] is True


def test_network_optin(fake_modal, tmp_path: Path) -> None:
    rt = _runtime(network=True)
    rt.setup("job1", tmp_path)
    assert _FakeSandbox.created_with["block_network"] is False


def test_shell_runs_in_work(fake_modal, tmp_path: Path) -> None:
    rt = _runtime()
    rt.setup("job1", tmp_path)
    res = rt.shell("pytest -q", tmp_path)
    assert res.passed
    last = fake_modal.execs[-1]
    assert last[0:2] == ("sh", "-c") and "cd /work && pytest -q" in last[2]


def test_sync_in_ships_repo_excluding_git(fake_modal, tmp_path: Path) -> None:
    (tmp_path / "keep.py").write_text("x = 1")
    (tmp_path / ".git").mkdir()
    (tmp_path / ".git" / "config").write_text("secret")
    rt = _runtime()
    rt.setup("job1", tmp_path)  # setup calls sync_in
    shipped = fake_modal.files["/tmp/in.tar"]
    names = tarfile.open(fileobj=io.BytesIO(shipped)).getnames()
    assert any("keep.py" in n for n in names)
    assert not any(".git" in n for n in names)


def test_sync_out_pulls_sandbox_files(fake_modal, tmp_path: Path) -> None:
    rt = _runtime()
    rt.setup("job1", tmp_path)
    rt.sync_out(tmp_path)
    assert (tmp_path / "sandbox_file.txt").read_bytes() == b"made-in-sandbox"


def test_teardown_terminates(fake_modal, tmp_path: Path) -> None:
    rt = _runtime()
    rt.setup("job1", tmp_path)
    rt.teardown()
    assert fake_modal.terminated


def test_get_runtime_modal_without_sdk_helpful_error(monkeypatch) -> None:
    monkeypatch.setitem(sys.modules, "modal", None)
    from lou_op.runtime import get_runtime

    with pytest.raises(ImportError, match="pip install modal"):
        get_runtime("modal")
