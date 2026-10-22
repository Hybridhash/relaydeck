"""The packaged runtime never ships or imports the demo fakes.

Test doubles live under `tests/support`; the dev web server lives in
`relaydeck.devtools`, outside the layers the native packager stages
(client, contracts, domain). Production client code must not import it.
"""

from __future__ import annotations

import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SRC = ROOT / "src" / "relaydeck"
STAGED_LAYERS = ("client", "contracts", "domain")  # what the stager copies
MOVED = ("fake_gateway", "fake_host", "fake_terminal", "scenarios", "fake")
DEV_ONLY_PREFIXES = ("relaydeck.devtools",)
SERVE = SRC / "client" / "serve.py"
SERVE_DEV_MODULE = "relaydeck.devtools.dev_server"


def _imports(path: Path) -> set[str]:
    names: set[str] = set()
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if isinstance(node, ast.Import):
            names.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            names.add(node.module)
            names.update(f"{node.module}.{alias.name}" for alias in node.names)
    return names


def test_demo_modules_are_not_in_any_staged_layer() -> None:
    for layer in STAGED_LAYERS:
        for path in (SRC / layer).rglob("*.py"):
            assert path.stem not in MOVED, f"{path} is a test double and belongs in tests/support"
    assert "devtools" not in STAGED_LAYERS


def test_client_modules_never_import_devtools_or_the_old_demo_paths() -> None:
    offenders: list[str] = []
    for path in (SRC / "client").rglob("*.py"):
        for name in _imports(path):
            if path == SERVE and name.startswith(SERVE_DEV_MODULE):
                continue  # the hosted server is never staged and wires the dev login
            old = any(name.endswith(f"adapters.{moved}") for moved in MOVED)
            if old or name.startswith(DEV_ONLY_PREFIXES):
                offenders.append(f"{path.relative_to(ROOT)}: {name}")
    assert offenders == [], offenders


def test_contracts_and_domain_do_not_import_devtools() -> None:
    for layer in ("contracts", "domain"):
        for path in (SRC / layer).rglob("*.py"):
            assert not any(n.startswith(DEV_ONLY_PREFIXES) for n in _imports(path)), path


def test_devtools_is_not_a_runtime_layer_of_the_boundary_test() -> None:
    boundary = (ROOT / "tests" / "architecture" / "test_import_boundaries.py").read_text()
    runtime_layers = boundary.split("RUNTIME_LAYERS = (")[1].split(")")[0]
    assert "devtools" not in runtime_layers
