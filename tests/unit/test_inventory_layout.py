"""Q6d: the inventory components package keeps its documented shape."""

from __future__ import annotations

import ast
import importlib
import pkgutil
from pathlib import Path

import pytest

import relaydeck.client.features.inventory.components as components

PKG_DIR = Path(components.__file__).parent
MAX_MODULE_LINES = 800
MAX_FUNCTION_LINES = 250

# Plain builder functions: a component boundary would add a control-tree node.
BUILDERS = {
    "shell_panes": (
        "build_sizing",
        "build_agents_pane",
        "build_shells_pane",
        "build_hosts_pane",
        "build_settings_page",
        "build_light_settings",
        "apply_collapsed_strip",
    ),
    "shell_frame": ("build_split", "build_body", "build_nav", "build_frame"),
    "chips": ("host_chips", "space_chips", "filter_field"),
    "state": ("use_filter_state", "use_sidebar_state"),
}


def _modules() -> list[str]:
    return [m.name for m in pkgutil.iter_modules(components.__path__)]


def test_old_single_module_is_gone_and_the_package_is_the_import_path() -> None:
    assert not (PKG_DIR.parent / "components.py").exists()
    assert (PKG_DIR / "__init__.py").is_file()


@pytest.mark.parametrize("name", _modules())
def test_every_module_has_a_docstring_and_stays_within_the_size_budget(name: str) -> None:
    path = PKG_DIR / f"{name}.py"
    tree = ast.parse(path.read_text())
    assert ast.get_docstring(tree), name
    assert len(path.read_text().splitlines()) <= MAX_MODULE_LINES, name
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
            first = min([node.lineno, *(d.lineno for d in node.decorator_list)])
            size = node.end_lineno - first + 1 if node.end_lineno else 0
            assert size <= MAX_FUNCTION_LINES, f"{name}.{node.name} is {size} lines"


def test_the_documented_modules_exist_and_the_docstring_lists_every_module() -> None:
    expected = {"models", "lists", "panes", "chips", "state", "context", "shell", "shell_panes"}
    expected |= {"shell_frame"}
    assert expected <= set(_modules())
    doc = components.__doc__ or ""
    for name in _modules():
        assert f"`{name}`" in doc, name


@pytest.mark.parametrize(("module", "names"), sorted(BUILDERS.items()))
def test_builders_and_hooks_are_plain_functions_not_components(
    module: str, names: tuple[str, ...]
) -> None:
    loaded = importlib.import_module(f"{components.__name__}.{module}")
    tree = ast.parse(Path(loaded.__file__ or "").read_text())
    defined = {n.name: n for n in tree.body if isinstance(n, ast.FunctionDef)}
    for name in names:
        assert name in defined, (module, name)
        for decorator in defined[name].decorator_list:
            assert "component" not in ast.unparse(decorator), (module, name)


def test_app_shell_is_the_only_component_that_calls_the_hooks_and_builders() -> None:
    shell = ast.parse((PKG_DIR / "shell.py").read_text())
    app = next(n for n in shell.body if isinstance(n, ast.FunctionDef) and n.name == "AppShell")
    assert any("component" in ast.unparse(d) for d in app.decorator_list)
    calls = [
        n.func.id for n in ast.walk(app) if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)
    ]
    # Hooks run unconditionally, filter hooks first, then the sidebar hooks.
    top = [
        n.value.func.id
        for n in app.body
        if isinstance(n, ast.Assign)
        and isinstance(n.value, ast.Call)
        and isinstance(n.value.func, ast.Name)
    ]
    assert top[:2] == ["use_filter_state", "use_sidebar_state"]
    for name in ("build_split", "build_body", "build_nav", "build_frame"):
        assert name in calls


def test_public_names_still_import_from_the_package() -> None:
    names = [
        "AgentsList", "AppShell", "ConnectionBanner", "DESTINATIONS", "DiagnosticsPane",
        "HostsList", "SearchField", "SessionDetail", "SettingsPane", "ShellsList", "StatusDot",
        "agent_scope_label", "host_context", "terminal_rows",
    ]  # fmt: skip
    for name in names:
        assert hasattr(components, name), name
        assert name in components.__all__, name
