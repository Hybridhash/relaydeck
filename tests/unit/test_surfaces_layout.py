"""Q6c: the terminal surfaces package keeps its documented shape."""

from __future__ import annotations

import ast
import importlib
import pkgutil
from pathlib import Path

import pytest

import relaydeck.client.features.terminal.surfaces as surfaces

PKG_DIR = Path(surfaces.__file__).parent
MAX_MODULE_LINES = 800
MAX_FUNCTION_LINES = 250
# Documented exceptions (see the module docstring of the owner).
FUNCTION_EXCEPTIONS = {"AgentTerminalScreen"}  # ~100-prop mount contract + forwarding

BUILDERS = (
    "agent_header",
    "terminal_host",
    "agent_dock",
    "agent_panels",
    "phone_dock",
    "input_area",
    "select_stack",
)


def _modules() -> list[str]:
    return [m.name for m in pkgutil.iter_modules(surfaces.__path__)]


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
            if node.name not in FUNCTION_EXCEPTIONS:
                assert size <= MAX_FUNCTION_LINES, f"{name}.{node.name} is {size} lines"


def test_the_documented_modules_exist() -> None:
    expected = {
        "sessions", "common", "actions", "direct_typing", "phone_composer",
        "agent_screen", "agent_hooks", "mount", *BUILDERS,
    }  # fmt: skip
    assert expected <= set(_modules())


def test_the_package_docstring_lists_every_module() -> None:
    doc = surfaces.__doc__ or ""
    for name in _modules():
        assert f"`{name}`" in doc, name


@pytest.mark.parametrize("name", BUILDERS)
def test_builders_are_plain_functions_not_components(name: str) -> None:
    """A component boundary would add a control-tree node and could remount."""
    module = importlib.import_module(f"{surfaces.__name__}.{name}")
    tree = ast.parse(Path(module.__file__ or "").read_text())
    for node in tree.body:
        if isinstance(node, ast.FunctionDef):
            for decorator in node.decorator_list:
                assert "component" not in ast.unparse(decorator), (name, node.name)


def test_public_names_still_import_from_the_package() -> None:
    names = [
        "mount_agent_screen", "AgentTerminalScreen", "DetailSessions", "SnoopingTerminalSocket",
        "selection_actions", "phone_selection_actions", "request_terminal_keyboard",
        "use_mobile_key_actions", "use_direct_keyboard", "use_paste_fallback",
        "direct_typing_allowed", "DirectTypingField", "drive_recovery", "offered_position",
        "gated_composer_send", "DRIVE_IDLE", "DRIVE_RESUMED", "DRIVE_REATTACHED",
        "DRIVE_REFUSED_EXITED", "EXIT_DETAILS", "SEND_BLOCKED_IME", "TAKEOVER_DISCLOSURE_AGENT",
        "TAKEOVER_MESSAGE", "PASTE_FALLBACK_HINT", "DirectInput", "AGENT_DETAIL_KEY",
        "AGENT_HEADER_KEY", "AGENT_KEYS_SCROLL_KEY", "AGENT_MORE_KEY", "AGENT_TITLE_KEY",
    ]  # fmt: skip
    for name in names:
        assert hasattr(surfaces, name), name
        assert name in surfaces.__all__, name


def test_typing_clock_lives_where_the_keyboard_hook_reads_it() -> None:
    from relaydeck.client.features.terminal.surfaces import direct_typing

    assert hasattr(direct_typing, "_typing_now")
    assert not hasattr(surfaces, "_typing_now")  # patching the package would do nothing
