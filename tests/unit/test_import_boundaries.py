"""Package boundary enforcement.

- relaydeck.domain imports stdlib only (no flet, no extensions, no client).
- relaydeck.contracts imports no UI or vendor modules.
- relaydeck.connector imports stdlib plus `cryptography` for enrollment
  keys. No UI, vendor frameworks, client, or gateway.
"""

from __future__ import annotations

import ast
from pathlib import Path

SRC = Path(__file__).resolve().parent.parent.parent / "src" / "relaydeck"

FORBIDDEN_DOMAIN = (
    "flet",
    "flet_secure_storage",
    "relaydeck_terminal",
    "relaydeck_notifications",
    "fastapi",
    "uvicorn",
    "relaydeck.client",
    "relaydeck.gateway",
    "relaydeck.connector",
)
FORBIDDEN_CONTRACTS = (
    "flet",
    "flet_secure_storage",
    "relaydeck_terminal",
    "relaydeck_notifications",
    "fastapi",
    "uvicorn",
    "relaydeck.client",
)
FORBIDDEN_CONNECTOR = (
    "flet",
    "flet_secure_storage",
    "relaydeck_terminal",
    "relaydeck_notifications",
    "fastapi",
    "uvicorn",
    "relaydeck.client",
    "relaydeck.gateway",
)


def _imports(path: Path) -> set[str]:
    tree = ast.parse(path.read_text())
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.update(a.name for a in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            names.add(node.module)
    return names


def _check(package: str, forbidden: tuple[str, ...]) -> None:
    root = SRC / package
    assert root.is_dir()
    for path in sorted(root.rglob("*.py")):
        for name in _imports(path):
            for ban in forbidden:
                assert not (name == ban or name.startswith(ban + ".")), (
                    f"{path.relative_to(SRC)} imports forbidden {name}"
                )


def test_domain_boundaries() -> None:
    _check("domain", FORBIDDEN_DOMAIN)


def test_contracts_boundaries() -> None:
    _check("contracts", FORBIDDEN_CONTRACTS)


def test_connector_boundaries() -> None:
    _check("connector", FORBIDDEN_CONNECTOR)


# the device app path must never import the web-serve seam (which
# needs flet_web/fastapi/uvicorn, unavailable in device builds).
FORBIDDEN_CLIENT_APP = (
    "relaydeck.client.serve",
    "flet_web",
    "fastapi",
    "uvicorn",
    "starlette",
)


def test_client_app_never_imports_serve() -> None:
    for path in sorted((SRC / "client").rglob("*.py")):
        if path.name == "serve.py":
            continue
        for name in _imports(path):
            for ban in FORBIDDEN_CLIENT_APP:
                assert not (name == ban or name.startswith(ban + ".")), (
                    f"{path.relative_to(SRC)} imports forbidden {name}"
                )
