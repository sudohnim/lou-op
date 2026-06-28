from __future__ import annotations

from pathlib import Path

from lou_op.models import FileWrite
from lou_op.protocol import (
    DONE_SENTINEL,
    has_done_sentinel,
    parse_files,
    render_files,
    write_files,
)


def test_parse_single_file():
    text = "<<<FILE app.py>>>\nprint('hi')\n<<<END>>>"
    files = parse_files(text)
    assert len(files) == 1
    assert files[0].path == "app.py"
    assert files[0].content == "print('hi')"


def test_parse_multiple_files_and_roundtrip():
    files = [FileWrite("a.py", "x = 1"), FileWrite("pkg/b.py", "y = 2")]
    parsed = parse_files(render_files(files))
    assert [(f.path, f.content) for f in parsed] == [
        ("a.py", "x = 1"),
        ("pkg/b.py", "y = 2"),
    ]


def test_done_sentinel():
    assert has_done_sentinel(f"all good\n{DONE_SENTINEL}\n")
    assert not has_done_sentinel("still working")


def test_write_files_creates_nested(tmp_path: Path):
    written = write_files(tmp_path, [FileWrite("pkg/mod.py", "z = 3")])
    assert written == ["pkg/mod.py"]
    assert (tmp_path / "pkg" / "mod.py").read_text() == "z = 3"


def test_write_rejects_escaping_path(tmp_path: Path):
    import pytest

    with pytest.raises(ValueError):
        write_files(tmp_path, [FileWrite("../evil.py", "bad")])
