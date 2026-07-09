"""Subprocess execution with watchdog timeouts and retry-with-backoff.

``run_command`` runs a fixed argv (git, etc.). ``run_shell`` runs a shell
string (user-supplied ``success_criteria`` commands). ``run_streaming`` adds a
watchdog that kills a process after either a total-time or silence timeout,
used to supervise long-running agent CLIs.
"""

from __future__ import annotations

import os
import re
import signal
import subprocess
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, List, Optional, Sequence, TypeVar

T = TypeVar("T")

# CSI sequences (colors, cursor moves, etc.) + OSC sequences (window title,
# hyperlink, etc.) that some test runners (vitest, playwright) emit even when
# stdout is piped. Leaking these into model context wastes tokens and confuses
# the loop. Also forces NO_COLOR/CI in scrubbed_env so well-behaved tools drop
# colors at the source.
_ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]|\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)|\x1b[=>].")


def _to_str(value: object) -> str:
    if value is None:
        return ""
    if isinstance(value, bytes):
        return value.decode(errors="replace")
    return str(value)


def strip_ansi(text: str) -> str:
    """Remove ANSI CSI/OSC/other escape sequences from text.

    Handles 256-color and truecolor SGR (`\x1b[38;5;123m`, `\x1b[38;2;r;g;bm`),
    cursor/edits, OSC hyperlinks (epoch.vimrs hết, iTerm), and a few legacy
    single-shift sequences that some test runners leak when stdout is a pipe
    but tty detection still engages.
    """
    return _ANSI_RE.sub("", text)


@dataclass
class CmdResult:
    returncode: int
    stdout: str
    stderr: str
    timed_out: bool

    @property
    def passed(self) -> bool:
        return self.returncode == 0 and not self.timed_out


# Model-influenced subprocesses (bash tool, validator shells, agent CLIs) get
# a strict ALLOWLIST, not a denylist — a denylist can't anticipate every
# secret a host carries (AWS_SECRET_ACCESS_KEY, GH_TOKEN, DATABASE_URL, ...).
# Provider HTTP calls read the API key in-process, never from this env.
_ENV_ALLOWED = {
    "PATH",
    "HOME",
    "USER",
    "LOGNAME",
    "SHELL",
    "TERM",
    "TMPDIR",
    "TZ",
    "LANG",
    "LANGUAGE",
    "PWD",
    "COLUMNS",
    "LINES",
    # python tooling needs these to find the right interpreter/site-packages
    "VIRTUAL_ENV",
    "PYTHONPATH",
    "PYTHONHASHSEED",
    "CI",
}
_ENV_ALLOWED_PREFIXES = ("LC_",)

# Well-behaved test runners (vitest, playwright, jest, mocha, pytest, cargo,
# ripgrep) respect these and drop color codes at the source. Saves the
# strip_ansi() regex pass from doing all the work, and keeps logs readable.
_FORCE_NO_COLOR = {"NO_COLOR": "1", "CLICOLOR": "0", "CLICOLOR_FORCE": "0", "TERM": "dumb"}


def scrubbed_env(passthrough: Sequence[str] = ()) -> dict:
    """Strict allowlist of os.environ; ``passthrough`` adds names to it.

    Adds NO_COLOR / CLICOLOR / TERM=dumb so model-influenced subprocesses
    (validators, agent bash tool) never emit ANSI escapes that waste context
    tokens or break log parsing.
    """
    keep = set(passthrough)
    env = {
        key: value
        for key, value in os.environ.items()
        if key in keep or key in _ENV_ALLOWED or key.startswith(_ENV_ALLOWED_PREFIXES)
    }
    env.update(_FORCE_NO_COLOR)
    return env


def run_command(cmd: Sequence[str], cwd: Path, *, timeout: int = 300) -> CmdResult:
    """Run a fixed argv with a hard timeout."""
    try:
        proc = subprocess.run(
            list(cmd),
            cwd=str(cwd),
            capture_output=True,
            text=True,
            timeout=timeout,
            stdin=subprocess.DEVNULL,
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

    Uses ``start_new_session=True`` so the shell and ALL its children
    (including backgrounded processes like ``foo &``) land in a dedicated
    process group. On timeout the entire group is killed — orphaned
    background servers can't hold stdout pipes open and hang the parent.
    ``stdin=DEVNULL`` prevents interactive prompts from blocking forever.
    """
    proc = subprocess.Popen(
        command,
        cwd=str(cwd),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        stdin=subprocess.DEVNULL,
        text=True,
        shell=True,
        env=env if env is not None else scrubbed_env(),
        start_new_session=True,
    )
    try:
        stdout, stderr = proc.communicate(timeout=timeout)
        return CmdResult(proc.returncode, stdout, stderr, False)
    except subprocess.TimeoutExpired:
        # Kill the entire process group — catches backgrounded children
        # (e.g. ``npx vite preview &``) that outlive the parent shell
        # and keep stdout pipes open, preventing communicate() from
        # returning.
        try:
            os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
        except ProcessLookupError:
            pass
        try:
            stdout, stderr = proc.communicate(timeout=5)
        except subprocess.TimeoutExpired:
            # SIGTERM didn't work — escalate to SIGKILL
            try:
                os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
            except ProcessLookupError:
                pass
            proc.wait()
            stdout, stderr = proc.communicate()
        return CmdResult(-1, _to_str(stdout), _to_str(stderr), True)


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
        stdin=subprocess.DEVNULL,
        text=True,
        bufsize=1,
        env=env if env is not None else scrubbed_env(),
        start_new_session=True,
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
                try:
                    os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
                except ProcessLookupError:
                    pass
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
