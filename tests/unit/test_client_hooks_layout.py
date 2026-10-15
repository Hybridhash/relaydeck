"""Q6b: `Root` is composed from the hooks package and keeps its documented shape."""

from __future__ import annotations

import ast
import importlib
import pkgutil
from pathlib import Path

import pytest

import relaydeck.client.app as app_module
import relaydeck.client.hooks as hooks

HOOKS_DIR = Path(hooks.__file__ or "").parent
APP = Path(app_module.__file__ or "")
MAX_MODULE_LINES = 800
MAX_FUNCTION_LINES = 250
# Documented exception: `Root` unpacks every hook result into the local names the render
# code already uses (kept so the render code stays untouched) and forwards inputs by keyword.
FUNCTION_EXCEPTIONS = {"Root"}

EXPECTED_HOOKS = {
    "appearance": {"use_appearance"},
    "preferences": {"use_preferences"},
    "navigation": {"use_route_state", "use_navigation", "use_route_effects"},
    "typing_focus": {"use_typing_focus"},
    "sync": {"use_sync_state", "use_sync_subscription", "use_host_effects"},
    "inventory": {"use_inventory"},
    "sheets": {"use_creation_sheets"},
    "retained": {"use_retained_sessions", "use_terminal_effects"},
}
BUILDERS = {
    "actions": {"build_open_terminal", "build_navigation_actions"},
    "creation": {"build_creation_actions", "build_shell_origin_actions"},
    "views": {
        "build_retained_views",
        "build_inventory_shell",
        "build_creation_sheets",
        "build_status_overlays",
    },
}


def _functions(path: Path) -> dict[str, ast.FunctionDef]:
    return {n.name: n for n in ast.parse(path.read_text()).body if isinstance(n, ast.FunctionDef)}


def _size(node: ast.FunctionDef) -> int:
    first = min([node.lineno, *(d.lineno for d in node.decorator_list)])
    return (node.end_lineno or first) - first + 1


def test_the_package_docstring_names_every_hook_and_builder() -> None:
    doc = hooks.__doc__ or ""
    for names in (*EXPECTED_HOOKS.values(), *BUILDERS.values()):
        for name in names:
            assert name in doc, name


@pytest.mark.parametrize("name", [m.name for m in pkgutil.iter_modules(hooks.__path__)])
def test_every_hooks_module_has_a_docstring_and_stays_within_budget(name: str) -> None:
    path = HOOKS_DIR / f"{name}.py"
    tree = ast.parse(path.read_text())
    assert ast.get_docstring(tree), name
    assert len(path.read_text().splitlines()) <= MAX_MODULE_LINES, name
    for fn_name, node in _functions(path).items():
        assert _size(node) <= MAX_FUNCTION_LINES, f"{name}.{fn_name}"


def test_app_module_budget_and_the_single_documented_exception() -> None:
    assert len(APP.read_text().splitlines()) <= MAX_MODULE_LINES
    for name, node in _functions(APP).items():
        if name not in FUNCTION_EXCEPTIONS:
            assert _size(node) <= MAX_FUNCTION_LINES, name


@pytest.mark.parametrize(("module", "names"), [*EXPECTED_HOOKS.items(), *BUILDERS.items()])
def test_the_documented_hooks_and_builders_exist(module: str, names: set[str]) -> None:
    funcs = _functions(HOOKS_DIR / f"{module}.py")
    assert names <= set(funcs), module


def test_hooks_are_plain_functions_not_components() -> None:
    """A component boundary would add a control-tree node and change ancestry."""
    for path in HOOKS_DIR.glob("*.py"):
        for node in _functions(path).values():
            for decorator in node.decorator_list:
                assert "component" not in ast.unparse(decorator), (path.name, node.name)


def test_hooks_never_import_the_app_module() -> None:
    for path in HOOKS_DIR.glob("*.py"):
        text = path.read_text()
        assert (
            "relaydeck.client.app " not in text and "from relaydeck.client import app" not in text
        )


def test_root_calls_its_hooks_unconditionally_at_the_top_level() -> None:
    """React-style rule: hook calls are never conditional (not under if/for/try/def)."""
    root = _functions(APP)["Root"]
    hook_names = {n for names in EXPECTED_HOOKS.values() for n in names}
    top_level_calls: set[str] = set()
    for stmt in root.body:
        for node in ast.walk(stmt):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
                if node.func.id in hook_names:
                    top_level_calls.add(node.func.id)
                    assert isinstance(stmt, ast.Assign | ast.Expr), node.func.id
    assert top_level_calls == hook_names


def test_public_names_still_import_from_the_app_module() -> None:
    for name in ("Root", "AppShell", "RETAINED_PARK_OFFSET", "new_command_id", "selection_host"):
        assert hasattr(app_module, name), name
    assert importlib.import_module("relaydeck.client.hooks.support").RETAINED_PARK_OFFSET == (
        app_module.RETAINED_PARK_OFFSET
    )
