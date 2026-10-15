"""Fixture viewer bit-rot regressions.

The committed viewer was unrunnable: FIXTURES used parents[5]
(IndexError outside the repo layout), Dropdown used on_change=
(TypeError under Flet 1.0.3), and the async read_selection was wired
through a sync lambda (dropped coroutine). All three are pinned here
through real host-harness renders.
"""

from __future__ import annotations

import inspect
import pathlib
from typing import Any

import pytest

from tests.support.host_harness import render_component, run

VIEWER_MAIN = (
    pathlib.Path(__file__).resolve().parents[3]
    / "extensions"
    / "terminal"
    / "examples"
    / "viewer"
    / "src"
    / "main.py"
)


def _load_viewer() -> Any:
    import importlib.util

    spec = importlib.util.spec_from_file_location("rd1t_viewer_main", VIEWER_MAIN)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def viewer() -> Any:
    return _load_viewer()


def test_viewer_renders_with_fixture_options(viewer: Any) -> None:
    async def scenario() -> None:
        assert viewer.FIXTURES.is_dir()
        app = await render_component(viewer.Viewer)
        assert app.find("status").value == "ready"
        options = app.find("fixture").options
        assert any(o.key == "01_ascii_colors.bin" for o in options)

    run(scenario())


def test_viewer_play_missing_fixture_reports_status(viewer: Any) -> None:
    async def scenario() -> None:
        app = await render_component(viewer.Viewer)
        await app.set_props_and_fire(
            "fixture", {"value": "no-such-fixture.bin"}, "select", "no-such-fixture.bin"
        )
        await app.set_props_and_fire("play", {}, "click")
        assert app.find("status").value.startswith("missing fixture:")

    run(scenario())


def test_viewer_selection_handler_is_awaited_coroutine(viewer: Any) -> None:
    async def scenario() -> None:
        app = await render_component(viewer.Viewer)
        handler = app.find("selection").on_click
        assert inspect.iscoroutinefunction(handler)

    run(scenario())


def test_viewer_fixture_discovery_has_no_fixed_depth(viewer: Any) -> None:
    source = VIEWER_MAIN.read_text()
    assert "parents[5]" not in source
    assert "on_change" not in source
