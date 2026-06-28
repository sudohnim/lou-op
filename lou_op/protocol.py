"""The raw-API file protocol and the completion sentinel.

Models on the raw-API path emit files as explicit delimiter blocks::

    <<<FILE path/to/file.py>>>
    ...contents...
    <<<END>>>

and signal completion with ``<lou-done/>``. The agent-CLI path writes files
directly and never uses this protocol; only ``has_done_sentinel`` is shared.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import List

from .models import FileWrite

DONE_SENTINEL = "<lou-done/>"

_FILE_RE = re.compile(
    r"<<<FILE\s+(?P<path>.+?)>>>\n(?P<body>.*?)\n?<<<END>>>",
    re.DOTALL,
)


def parse_files(text: str) -> List[FileWrite]:
    """Extract all ``<<<FILE>>>`` blocks from ``text``."""
    files: List[FileWrite] = []
    for match in _FILE_RE.finditer(text):
        path = match.group("path").strip()
        body = match.group("body")
        if path:
            files.append(FileWrite(path=path, content=body))
    return files


def render_files(files: List[FileWrite]) -> str:
    """Inverse of :func:`parse_files` (used by the mock backend and tests)."""
    blocks = [f"<<<FILE {f.path}>>>\n{f.content}\n<<<END>>>" for f in files]
    return "\n".join(blocks)


def has_done_sentinel(text: str) -> bool:
    return DONE_SENTINEL in text


def write_files(repo_path: Path, files: List[FileWrite]) -> List[str]:
    """Write ``files`` under ``repo_path``; return the relative paths written.

    Refuses paths that escape the repo root.
    """
    written: List[str] = []
    root = repo_path.resolve()
    for file in files:
        target = (root / file.path).resolve()
        if not str(target).startswith(str(root)):
            raise ValueError(f"path escapes repo: {file.path}")
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(file.content, encoding="utf-8")
        written.append(file.path)
    return written
