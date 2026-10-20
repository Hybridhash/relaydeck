"""a native edit in the draft box survives a parent render that still carries
the older draft, and a model change (Send cleared it) still renders. Synthetic host only.
"""

from __future__ import annotations

from typing import Any

import flet as ft
import pytest

from relaydeck.client.features.composer.components import (
    PROGRESS_HINT,
    VALIDATION_KEY,
    ComposerBox,
)
from relaydeck.client.features.composer.domain import BLOCKER_EMPTY
from tests.support.host_harness import render_component, run


@ft.component
def _Parent(props: dict[str, Any]) -> Any:
    """A stateful parent: its own state change re-renders it with the current `props`."""
    rev, set_rev = ft.use_state(0)
    return ft.Column(
        controls=[
            ft.Button("bump", key="bump", on_click=lambda _e: set_rev(rev + 1)),
            ComposerBox(**props),
        ]
    )


def _props(draft: str) -> dict[str, Any]:
    return {
        "draft": draft,
        "markers": (),
        "can_send": bool(draft.strip()),
        "blockers": (),
        "submission": "idle",
        "error": None,
        "review": None,
        "input_mode": "text",
        "on_draft_change": lambda _t: None,
        "on_send": lambda: None,
        "on_confirm_blocked": lambda: None,
        "on_cancel_blocked": lambda: None,
        "on_clear_error": lambda: None,
    }


async def _rerender(app: Any, props: dict[str, Any]) -> None:
    """Re-render the same parent instance with `props` (refs survive)."""
    app.props_box.clear()
    app.props_box.update(props)
    await app.set_props_and_fire("bump", {}, "click")


def test_native_paste_survives_a_parent_render_with_the_older_draft() -> None:
    async def scenario() -> None:
        box = _props("old")
        app = await render_component(_Parent, box)
        app.props_box = box
        # The native field receives a paste; on_change has not round-tripped yet.
        app.session.apply_patch(app.find("composer-draft")._i, {"value": "old pasted"})
        await _rerender(app, _props("old"))
        assert app.find("composer-draft").value == "old pasted"

    run(scenario())


def test_model_change_from_the_server_renders_the_draft() -> None:
    async def scenario() -> None:
        box = _props("old")
        app = await render_component(_Parent, box)
        app.props_box = box
        app.session.apply_patch(app.find("composer-draft")._i, {"value": "old pasted"})
        # Send cleared the draft: the model moved, so the field clears.
        await _rerender(app, _props(""))
        assert app.find("composer-draft").value == ""

    run(scenario())


def test_untouched_field_keeps_rendering_the_model_draft() -> None:
    async def scenario() -> None:
        box = _props("hello")
        app = await render_component(_Parent, box)
        app.props_box = box
        await _rerender(app, _props("hello"))
        assert app.find("composer-draft").value == "hello"
        await _rerender(app, _props("hello world"))
        assert app.find("composer-draft").value == "hello world"

    run(scenario())


@pytest.mark.parametrize("fill_lines", [3, None])
def test_progress_notice_ends_when_the_agent_stops(fill_lines: int | None) -> None:
    async def scenario() -> None:
        props = {**_props("next"), "submission": "progress", "fill_lines": fill_lines}
        app = await render_component(_Parent, {**props, "working": False})
        if fill_lines:
            assert app.find(VALIDATION_KEY).content.value == ""
        with pytest.raises(KeyError):
            app.find("composer-progress")

    run(scenario())


@pytest.mark.parametrize(
    "submission,shown", [("idle", "Write a message to send."), ("progress", PROGRESS_HINT)]
)
def test_fixed_box_shows_one_status_line_in_a_reserved_row(submission: str, shown: str) -> None:
    async def scenario() -> None:
        props = {
            **_props(""),
            "blockers": (BLOCKER_EMPTY,),
            "submission": submission,
            "fill_lines": 3,
        }
        app = await render_component(_Parent, props)
        field = app.find("composer-draft")
        assert (field.min_lines, field.max_lines) == (3, 3)
        row = app.find(VALIDATION_KEY)
        line = row.content.content if isinstance(row.content, ft.Shimmer) else row.content
        assert row.height and line.value == shown
        assert isinstance(row.content, ft.Shimmer) is (submission == "progress")
        with pytest.raises(KeyError):
            app.find("composer-progress")

    run(scenario())
