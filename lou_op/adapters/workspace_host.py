"""HostWorkspace: the working tree is a local directory; exec is a local
process in its own process group so deadline breaches are killable (I8).

Git parsing uses ``status -z`` (NUL-delimited, unquoted) — text porcelain
quotes filenames with spaces and merges rename fields, which silently broke
the old revert path.
"""

from __future__ import annotations

import os
import signal
import subprocess
import time
from pathlib import Path
from typing import List, Optional

from ..exec import scrubbed_env
from ..ports.workspace import Changed, ExecResult, Snapshot, Workspace


def _git(root: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["git", *args], cwd=root, capture_output=True, text=True)


class HostWorkspace(Workspace):
    def __init__(self, root: Path) -> None:
        self.root = Path(root)

    def exec(
        self,
        command: str,
        *,
        timeout: int = 300,
        deadline: Optional[float] = None,
    ) -> ExecResult:
        budget = float(timeout)
        if deadline is not None:
            budget = max(0.0, min(budget, deadline - time.monotonic()))

        proc = subprocess.Popen(
            command,
            shell=True,
            cwd=self.root,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            stdin=subprocess.DEVNULL,  # ← Prevents interactive prompts
            text=True,
            env=scrubbed_env(),
            start_new_session=True,  # ← Own process group → killable as a unit
        )
        try:
            out, err = proc.communicate(timeout=budget)
            return ExecResult(proc.returncode, out, err)
        except subprocess.TimeoutExpired:
            # hard cancel: SIGKILL the whole group — trap-ignoring children
            # and grandchildren die with it (I8)
            try:
                os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
            except (ProcessLookupError, PermissionError):
                proc.kill()

            # Wait for all descendants to die and collect output
            out, err = proc.communicate()
            return ExecResult(-9, out or "", err or "", timed_out=True, killed=True)

    def changed_paths(self) -> List[Changed]:
        res = _git(self.root, "status", "-z", "--porcelain")
        entries: List[Changed] = []
        fields = res.stdout.split("\0")
        i = 0
        while i < len(fields):
            entry = fields[i]
            if len(entry) < 4:
                i += 1
                continue
            code, path = entry[:2], entry[3:]
            if code.startswith("R") or code.startswith("C"):
                # NUL-split: next field is the rename/copy SOURCE
                old = fields[i + 1] if i + 1 < len(fields) else None
                entries.append(
                    Changed(path=path, status="renamed", old_path=old, staged=True)
                )
                i += 2
                continue
            if code == "??":
                entries.append(Changed(path=path, status="untracked"))
            elif "D" in code:
                entries.append(
                    Changed(path=path, status="deleted", staged=code[0] != " ")
                )
            elif "A" in code:
                entries.append(Changed(path=path, status="added", staged=True))
            else:
                entries.append(
                    Changed(path=path, status="modified", staged=code[0] != " ")
                )
            i += 1
        return entries

    def _in_head(self, rel: str) -> bool:
        return _git(self.root, "cat-file", "-e", f"HEAD:{rel}").returncode == 0

    def restore_paths(self, paths: List[str]) -> None:
        for rel in paths:
            if self._in_head(rel):
                _git(self.root, "checkout", "HEAD", "--", rel)
            else:
                # never existed in HEAD: unstage and remove from disk
                _git(self.root, "rm", "-f", "--cached", "--ignore-unmatch", rel)
                target = self.root / rel
                if target.is_file() or target.is_symlink():
                    target.unlink()

    def snapshot(self) -> Snapshot:
        head = _git(self.root, "rev-parse", "HEAD").stdout.strip()
        untracked = [c.path for c in self.changed_paths() if c.status == "untracked"]
        return Snapshot(ref=head, untracked=untracked)

    def restore(self, snap: Snapshot) -> None:
        _git(self.root, "reset", "--hard", snap.ref)
        # drop untracked files created after the snapshot
        for change in self.changed_paths():
            if change.status == "untracked" and change.path not in snap.untracked:
                target = self.root / change.path
                if target.is_file() or target.is_symlink():
                    target.unlink()

    def commit(self, message: str, author: str) -> str:
        _git(self.root, "add", "-A")
        staged = _git(self.root, "diff", "--cached", "--quiet")
        if staged.returncode == 0:
            return ""  # nothing to commit
        res = _git(
            self.root,
            "commit",
            "-m",
            message,
            f"--author={author}",
        )
        if res.returncode != 0:
            return ""
        return _git(self.root, "rev-parse", "HEAD").stdout.strip()
