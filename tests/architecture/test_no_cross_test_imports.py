"""Test modules never import each other.

Helpers shared by several tests live in ``tests/support/<area>/`` (or the
older ``tests/support`` modules) and every test imports them from there. A test
module imported from another test is collected twice by name confusion, couples
unrelated suites, and makes renaming a test file a breaking change.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

TESTS = Path(__file__).resolve().parents[1]
SOURCES = sorted(p for p in TESTS.rglob("*.py") if "__pycache__" not in p.parts)


def _is_test_module(name: str) -> bool:
    return name.rsplit(".", 1)[-1].startswith("test_")


def _violations(path: Path) -> list[str]:
    found: list[str] = []
    for node in ast.walk(ast.parse(path.read_text())):
        if isinstance(node, ast.Import):
            found += [
                f"line {node.lineno}: import {a.name}"
                for a in node.names
                if a.name.split(".")[0] == "tests" and _is_test_module(a.name)
            ]
        elif isinstance(node, ast.ImportFrom):
            module = ("." * node.level) + (node.module or "")
            if _is_test_module(module):
                found.append(f"line {node.lineno}: from {module} import ...")
            # `from tests.ui import test_x` / `from . import test_x`
            found += [
                f"line {node.lineno}: from {module or '.'} import {a.name}"
                for a in node.names
                if (node.level or (node.module or "").startswith("tests"))
                and _is_test_module(a.name)
            ]
    return found


@pytest.mark.parametrize("path", SOURCES, ids=lambda p: p.relative_to(TESTS).as_posix())
def test_module_does_not_import_a_test_module(path: Path) -> None:
    assert _violations(path) == [], (
        f"{path.relative_to(TESTS)} imports another test module; "
        "move the shared helper to tests/support/<area>/"
    )


def test_the_detector_flags_every_import_form(tmp_path: Path) -> None:
    sample = tmp_path / "sample.py"
    sample.write_text(
        "from tests.ui.test_a import x\n"
        "import tests.unit.test_b\n"
        "from . import test_c\n"
        "from .test_d import y\n"
        "from tests.ui import test_e\n"
        "from tests.support.ui.navigation import ok\n"
    )
    assert len(_violations(sample)) == 5
