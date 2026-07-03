"""Subprocess execution with watchdog timeouts and retry-with-backoff.

``run_command`` runs a fixed argv (git, etc.). ``run_shell`` runs a shell
string (user-supplied ``success_criteria`` commands). ``run_streaming`` adds a
watchdog that kills a process after either a total-time or silence timeout,
used to supervise long-running agent CLIs.
"""

from __future__ import annotations

import os
import subprocess
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, List, Optional, Sequence, TypeVar

T = TypeVar("T")


def _to_str(value: object) -> str:
    if value is None:
        return ""
    if isinstance(value, bytes):
        return value.decode(errors="replace")
    return str(value)


@dataclass
class CmdResult:
    returncode: int
    stdout: str
    stderr: str
    timed_out: bool

    @property
    def passed(self) -> bool:
        return self.returncode == 0 and not self.timed_out


# Secrets never exposed to model-influenced subprocesses (bash tool, validator
# shells, agent CLIs). Provider HTTP calls read the key in-process, not env.
_SECRET_KEYS = {"OPENROUTER_API_KEY", "ANTHROPIC_API_KEY"}
_SECRET_PREFIXES = ("LOU_",)


def scrubbed_env(passthrough: Sequence[str] = ()) -> dict:
    """os.environ minus API keys and LOU_* config; ``passthrough`` names win."""
    keep = set(passthrough)
    return {
        key: value
        for key, value in os.environ.items()
        if key in keep
        or (key not in _SECRET_KEYS and not key.startswith(_SECRET_PREFIXES))
    }


def run_command(cmd: Sequence[str], cwd: Path, *, timeout: int = 300) -> CmdResult:
    """Run a fixed argv with a hard timeout."""
    try:
        proc = subprocess.run(
            list(cmd),
            cwd=str(cwd),
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        return CmdResult(proc.returncode, proc.stdout, proc.stderr, False)
    except subprocess.TimeoutExpired as exc:
        return CmdResult(-1, _to_str(exc.stdout), _to_str(exc.stderr), True)


def run_shell(
    command: str,
    cwd: Path,
    *,
    timeout: int = 300,
    env: Optional[dict] = None,
) -> CmdResult:
    """Run a shell command string with a hard timeout.

    Model-influenced by default (validator criteria, agent bash), so the
    environment is scrubbed of secrets unless an explicit ``env`` is given.
    """
    try:
        proc = subprocess.run(
            command,
            cwd=str(cwd),
            capture_output=True,
            text=True,
            timeout=timeout,
            shell=True,
            env=env if env is not None else scrubbed_env(),
        )
        return CmdResult(proc.returncode, proc.stdout, proc.stderr, False)
    except subprocess.TimeoutExpired as exc:
        return CmdResult(-1, _to_str(exc.stdout), _to_str(exc.stderr), True)


def run_streaming(
    cmd: Sequence[str],
    cwd: Path,
    *,
    total_timeout: int = 600,
    silence_timeout: int = 300,
    on_line: Optional[Callable[[str], None]] = None,
    env: Optional[dict] = None,
) -> CmdResult:
    """Run ``cmd``, streaming stdout, killing it if it hangs.

    The watchdog kills the process when either the total runtime exceeds
    ``total_timeout`` or no output appears for ``silence_timeout`` seconds.
    """
    proc = subprocess.Popen(
        list(cmd),
        cwd=str(cwd),
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        bufsize=1,
        env=env if env is not None else scrubbed_env(),
    )
    lines: List[str] = []
    start = time.monotonic()
    last_output = [start]
    timed_out = [False]
    stop = threading.Event()

    def watchdog() -> None:
        while not stop.wait(1.0):
            now = time.monotonic()
            if (now - start > total_timeout) or (
                now - last_output[0] > silence_timeout
            ):
                timed_out[0] = True
                proc.kill()
                return

    watcher = threading.Thread(target=watchdog, daemon=True)
    watcher.start()

    if proc.stdout is not None:
        for line in proc.stdout:
            last_output[0] = time.monotonic()
            lines.append(line)
            if on_line is not None:
                on_line(line)

    proc.wait()
    stop.set()
    watcher.join(timeout=2.0)
    return CmdResult(proc.returncode, "".join(lines), "", timed_out[0])


def retry_with_backoff(
    fn: Callable[[], T],
    *,
    retries: int = 3,
    delays: Sequence[float] = (0.0, 5.0, 15.0),
) -> T:
    """Call ``fn``; retry on any exception with the given backoff delays."""
    last_exc: Optional[BaseException] = None
    for attempt in range(max(1, retries)):
        try:
            return fn()
        except Exception as exc:  # noqa: BLE001 - retry any transient failure
            last_exc = exc
            delay = delays[min(attempt, len(delays) - 1)]
            if delay > 0:
                time.sleep(delay)
    assert last_exc is not None
    raise last_exc
