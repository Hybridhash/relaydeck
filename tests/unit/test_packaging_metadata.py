"""the packaging metadata is installable outside the checkout (static, offline).

The install itself is proven by scripts/check_dist_install.sh, which builds the wheel and installs
it into a temporary venv. These tests keep the declarations honest between runs: no path dependency
in the distribution, Python 3.13, every runtime import declared, and the extension packages inside
the wheel.
"""

from __future__ import annotations

import ast
import re
import sys
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
RUNTIME_PACKAGES = ("companion", "gateway", "connector", "client", "usage")
IN_WHEEL = {"relaydeck", "relaydeck_terminal", "relaydeck_notifications"}
# Import name -> the distribution that provides it (the names used in pyproject dependencies).
DISTRIBUTION = {
    "PIL": "pillow",
    "anyio": "anyio",
    "cryptography": "cryptography",
    "fastapi": "fastapi",
    "flet": "flet",
    "flet_secure_storage": "flet-secure-storage",
    "flet_web": "flet-web",
    "httpx": "httpx",
    "msgpack": "msgpack",
    "qrcode": "qrcode",
    "starlette": "starlette",
    "uvicorn": "uvicorn",
    "websockets": "websockets",
}


def _pyproject() -> dict:
    return tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))


def _normal(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


def _all_dependencies() -> list[str]:
    project = _pyproject()["project"]
    extras = project.get("optional-dependencies", {})
    return [*project["dependencies"], *(entry for group in extras.values() for entry in group)]


def _declared_names(dependencies: list[str]) -> set[str]:
    names = set()
    for entry in dependencies:
        names.add(_normal(re.split(r"[<>=!~\[; @]", entry, maxsplit=1)[0]))
    return names


def _runtime_third_party_imports() -> dict[str, set[str]]:
    found: dict[str, set[str]] = {}
    sources = [ROOT / "src" / "relaydeck" / "cli.py"]
    for package in RUNTIME_PACKAGES:
        sources.extend((ROOT / "src" / "relaydeck" / package).rglob("*.py"))
    stdlib = set(sys.stdlib_module_names)
    for path in sources:
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            names: list[str] = []
            if isinstance(node, ast.Import):
                names = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
                names = [node.module]
            for name in names:
                top = name.split(".")[0]
                if top in stdlib or top in IN_WHEEL:
                    continue
                found.setdefault(top, set()).add(str(path.relative_to(ROOT)))
    return found


def test_the_distribution_declares_no_path_or_file_dependency() -> None:
    for entry in _all_dependencies():
        assert "path" not in entry.lower(), entry
        assert "@" not in entry and "file:" not in entry, entry
    for group, entries in _pyproject()["dependency-groups"].items():
        for entry in entries:
            if isinstance(entry, str):
                assert "path =" not in entry, (group, entry)


def test_the_extension_packages_are_inside_the_wheel_and_not_path_dependencies() -> None:
    wheel = _pyproject()["tool"]["hatch"]["build"]["targets"]["wheel"]
    packages = wheel["packages"]
    assert "src/relaydeck" in packages
    assert "extensions/terminal/src/relaydeck_terminal" in packages
    assert "extensions/notifications/src/relaydeck_notifications" in packages
    declared = _declared_names(_all_dependencies())
    assert "relaydeck-terminal" not in declared
    assert "relaydeck-notifications" not in declared


def test_the_companion_extra_carries_the_server_packages_and_the_mobile_list_does_not() -> None:
    extra = _declared_names(_pyproject()["project"]["optional-dependencies"]["companion"])
    base = _declared_names(_pyproject()["project"]["dependencies"])
    assert {"fastapi", "uvicorn", "qrcode", "pillow", "msgpack"} <= extra
    assert not extra & base


def test_requires_python_is_exactly_three_thirteen() -> None:
    assert _pyproject()["project"]["requires-python"] == ">=3.13,<3.14"


def test_every_third_party_runtime_import_is_a_declared_dependency() -> None:
    declared = _declared_names(_all_dependencies())
    imports = _runtime_third_party_imports()
    unmapped = sorted(name for name in imports if name not in DISTRIBUTION)
    assert not unmapped, f"add these imports to DISTRIBUTION and to the dependencies: {unmapped}"
    missing = {
        name: sorted(imports[name])
        for name in imports
        if _normal(DISTRIBUTION[name]) not in declared
    }
    assert not missing, f"runtime imports without a declared dependency: {missing}"


def test_the_dev_group_keeps_the_editable_extensions_for_the_source_checkout() -> None:
    dev = _pyproject()["dependency-groups"]["dev"]
    assert "relaydeck-terminal" in dev and "relaydeck-notifications" in dev
    sources = _pyproject()["tool"]["uv"]["sources"]
    assert sources["relaydeck-terminal"]["path"] == "extensions/terminal"
    assert sources["relaydeck-notifications"]["path"] == "extensions/notifications"
