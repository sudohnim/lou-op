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

_SCRATCHPAD_RE = re.compile(
    r"(?:<<<SCRATCHPAD>>>|<SCRATCHPAD>)\n(?P<body>.*?)\n?(?:<<<END>>>|</SCRATCHPAD>)",
    re.DOTALL,
)


_MD_FENCE_RE = re.compile(r"^```[^\n]*\n", re.MULTILINE)

# Matches any mention of a filename near a code block:
# "### File: store.py", "## store.py:", "**store.py**", "in `store.py`", "the `store.py` file"
_MD_HEADER_FILE_RE = re.compile(
    r"(?:#{1,4}\s+(?:File:\s*|Implementation\s+of\s+)?|File:\s+|\*\*|`|in\s+`|the\s+`)"
    r"(?P<path>[\w/.-]+\.(?:py|txt|yaml|yml|json|toml|sh|md))\b",
    re.IGNORECASE,
)
# Matches "# filename.py" as first line inside a code block
_COMMENT_FILE_RE = re.compile(
    r"^#\s*(?P<path>[\w/.-]+\.(?:py|txt|yaml|yml|json|toml|sh|md))\s*$"
)
# Extracts raw code blocks (``` ... ```)
_CODE_BLOCK_RE = re.compile(r"```(?:\w+)?\n(?P<body>.*?)```", re.DOTALL)


def _parse_files_markdown_fallback(text: str) -> List[FileWrite]:
    """Fallback for models that use markdown headers + code blocks instead of <<<FILE>>>."""
    files: List[FileWrite] = []
    seen: set = set()
    for block in _CODE_BLOCK_RE.finditer(text):
        body = block.group("body")
        start = block.start()
        path = None

        # Check preceding 300 chars for a header naming this file (use last match)
        preceding = text[max(0, start - 300) : start]
        headers = list(_MD_HEADER_FILE_RE.finditer(preceding))
        if headers:
            path = headers[-1].group("path")
        else:
            # Check first line of block for "# filename.py"
            first_line = body.split("\n")[0].strip()
            cm = _COMMENT_FILE_RE.match(first_line)
            if cm:
                path = cm.group("path")
                body = "\n".join(body.split("\n")[1:])

        if path and path not in seen:
            seen.add(path)
            files.append(FileWrite(path=path, content=body.rstrip()))

    return files


def parse_files(text: str) -> List[FileWrite]:
    """Extract file writes from model output.

    Tries ``<<<FILE>>>`` blocks first (preferred format). Falls back to
    markdown code blocks with filename headers for models that ignore the
    delimiter format.
    """
    stripped = _MD_FENCE_RE.sub("", text).replace("\n```", "")
    files: List[FileWrite] = []
    for match in _FILE_RE.finditer(stripped):
        path = match.group("path").strip()
        body = match.group("body")
        if path:
            files.append(FileWrite(path=path, content=body))
    if not files:
        files = _parse_files_markdown_fallback(text)
    return files


def render_files(files: List[FileWrite]) -> str:
    """Inverse of :func:`parse_files` (used by the mock backend and tests)."""
    blocks = [f"<<<FILE {f.path}>>>\n{f.content}\n<<<END>>>" for f in files]
    return "\n".join(blocks)


def parse_scratchpad(text: str) -> str:
    """Extract the <<<SCRATCHPAD>>> block from model output, or return ''."""
    match = _SCRATCHPAD_RE.search(text)
    return match.group("body").strip() if match else ""


def has_done_sentinel(text: str) -> bool:
    return DONE_SENTINEL in text


def write_files(repo_path: Path, files: List[FileWrite]) -> List[str]:
    """Write ``files`` under ``repo_path``; return the relative paths written.

    Refuses paths that escape the repo root.
    """
    written: List[str] = []
    root = repo_path.resolve()
    for file in files:
        # is_relative_to, not a prefix check — /x/repo-evil must not pass
        # for root /x/repo. resolve() follows symlinks first.
        target = (root / file.path).resolve()
        if not target.is_relative_to(root):
            raise ValueError(f"path escapes repo: {file.path}")
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(file.content, encoding="utf-8")
        written.append(file.path)
    return written
