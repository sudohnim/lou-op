"""P7 spec (I6): dependency direction is enforced, not hoped for.

domain/ and ports/ import only the standard library and each other —
never adapters, backends, config, orchestration, or third-party
frameworks. This test is the import-linter: it fails the moment anyone
adds an outward import, which is how layering survives future changes.
"""

from __future__ import annotations

import ast
import sys
from pathlib import Path

import pytest

PKG = Path(__file__).parent.parent / "lou_op"

# what the pure layers may import
_ALLOWED_PREFIXES = ("lou_op.domain", "lou_op.ports")
_FORBIDDEN_THIRD_PARTY = {"httpx", "yaml", "pydantic", "fastapi", "dotenv", "modal"}


def _module_name(path: Path) -> str:
    rel = path.relative_to(PKG.parent)
    return ".".join(rel.with_suffix("").parts)


def _imports(path: Path) -> list[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    out = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            out.extend(a.name for a in node.names)
        elif isinstance(node, ast.ImportFrom):
            if node.level and node.module is not None:
                # resolve relative import against the file's package
                pkg_parts = _module_name(path).split(".")[: -node.level]
                out.append(".".join(pkg_parts + node.module.split(".")))
            elif node.module:
                out.append(node.module)
    return out


def _pure_layer_files() -> list[Path]:
    return sorted(
        p for layer in ("domain", "ports") for p in (PKG / layer).glob("*.py")
    )


@pytest.mark.parametrize("path", _pure_layer_files(), ids=lambda p: p.name)
def test_pure_layer_imports_nothing_outward(path: Path) -> None:
    violations = []
    for name in _imports(path):
        if name.startswith("lou_op"):
            if not name.startswith(_ALLOWED_PREFIXES):
                violations.append(name)
        else:
            root = name.split(".")[0]
            if root in _FORBIDDEN_THIRD_PARTY:
                violations.append(name)
            elif root not in sys.stdlib_module_names:
                violations.append(name)
    assert not violations, (
        f"{path.name} imports outward: {violations} — the pure layer may"
        " import only stdlib, lou_op.domain, lou_op.ports"
    )


def test_layers_exist_and_are_nonempty() -> None:
    files = _pure_layer_files()
    assert len(files) >= 6  # domain: graph/iteration/scope/verification; ports: 3+
