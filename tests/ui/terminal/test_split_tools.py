"""Typing-column tools use the guarded writer and stay inside the visible terminal."""

from __future__ import annotations

import asyncio
import struct
from importlib import import_module
from typing import Any

import flet as ft
import pytest
from relaydeck_terminal import protocol

from relaydeck.client.features.terminal.input_modes import (
    KEYBOARD_KEYS,
    WORKFLOW_AGENT_COMPOSER,
    WORKFLOW_AGENT_DIRECT,
)
from relaydeck.client.features.terminal.key_bar import KEY_BAR_GROUPS
from relaydeck.client.features.terminal.surfaces import mount_agent_screen
from relaydeck.contracts.wire import terminal as terminal_wire
from tests.support.host_harness import render_component, run
from tests.support.terminal.binding import _wait_for
from tests.support.terminal.selection import _mark_ready, _snapshot_requests
from tests.support.terminal.sessions import _agent_session
from tests.support.ui.tree_path import _path_to, _signature


def _has(app: Any, key: str) -> bool:
    try:
        app.find(key)
    except KeyError:
        return False
    return True


@pytest.mark.parametrize("width,height", [(1032, 560), (1376, 396)])
def test_split_tools_actions_preserve_the_terminal_and_draft(
    monkeypatch: pytest.MonkeyPatch, width: int, height: int
) -> None:
    attached: list[bool] = []
    original = import_module(
        "relaydeck.client.features.terminal.surfaces.mount"
    ).use_picture_attachment

    def pictures(*args: Any, **kwargs: Any) -> dict[str, Any]:
        props = original(*args, **kwargs)
        props.update(on_attach_picture=lambda: attached.append(True), attach_enabled=True)
        return props

    monkeypatch.setattr(
        "relaydeck.client.features.terminal.surfaces.mount.use_picture_attachment",
        pictures,
    )

    async def scenario() -> None:
        session, _ops, factory, _submitted = _agent_session()
        session.open()
        await session.confirm_takeover()
        session.on_draft_change("unsent draft")
        session.set_keyboard(KEYBOARD_KEYS)
        kept: list[bool] = []
        focus: list[bool] = []
        app = await render_component(
            mount_agent_screen,
            session,
            width_tier="expanded",
            viewport_height=height,
            viewport_width=width,
            typing_split=True,
            keyboard_up=True,
            touch=True,
            on_keep_typing=lambda: kept.append(True),
            on_composer_focus=focus.append,
        )
        try:
            await app.set_props_and_fire(
                "agent-detail-panel", {}, "size_change", {"w": width, "h": height}
            )
            await app.set_props_and_fire(
                "terminal-flex-host", {}, "size_change", {"w": width * 0.7, "h": height - 80}
            )
            term, sent = _mark_ready(session)
            path = _signature(_path_to(app, "terminal-surface"))
            field_id = app.find("composer-draft")._i
            assert not _has(app, "agent-keys-toggle")
            assert not _has(app, "agent-split-dock")
            assert not _has(app, "agent-keys-scroll")
            assert not _has(app, "terminal-shortcuts")
            toggle = app.find("agent-tools-toggle")
            assert toggle.icon == ft.Icons.KEYBOARD_COMMAND_KEY
            semantics = next(
                c for c in _path_to(app, "agent-tools-toggle") if isinstance(c, ft.Semantics)
            )
            assert semantics.label == "Keys and actions" and not semantics.expanded
            await app.set_props_and_fire("agent-tools-toggle", {}, "click")
            assert app.find("tools-popover").visible
            assert app.find("agent-tools-toggle-semantics").expanded
            assert app.find("composer-draft")._i == field_id
            assert app.find("composer-draft").can_request_focus
            assert focus == [] and kept == [True]
            panel = app.find("tools-popover-panel")
            assert panel.top >= 32 and panel.bottom >= 0
            assert panel.right > app.find("agent-composer-slot").width
            assert height - panel.top - panel.bottom <= height - 80
            assert app.find("tools-popover-scroll").scroll == ft.ScrollMode.AUTO
            for group in KEY_BAR_GROUPS:
                for item in group:
                    if item.kind != "action":
                        assert _has(app, f"tools-{item.id}")
            before = len(factory.sockets[-1].sent_bytes)
            await app.set_props_and_fire("tools-key-esc", {}, "click")
            frames = factory.sockets[-1].sent_bytes[before:]
            assert [terminal_wire.decode_input(raw).data for raw in frames] == [b"\x1b"]
            await app.set_props_and_fire("tools-popover-close", {}, "click")
            assert not app.find("tools-popover").visible
            await app.set_props_and_fire("agent-tools-toggle", {}, "click")
            await app.set_props_and_fire("tools-attach-picture", {}, "click")
            assert attached == [True] and not app.find("tools-popover").visible
            await app.set_props_and_fire("agent-tools-toggle", {}, "click")
            await app.set_props_and_fire("tools-mode-direct", {}, "click")
            assert session.view().workflow == WORKFLOW_AGENT_DIRECT
            await app.set_props_and_fire("agent-tools-toggle", {}, "click")
            await app.set_props_and_fire("tools-mode-compose", {}, "click")
            assert session.view().workflow == WORKFLOW_AGENT_COMPOSER
            session._clipboard.text = "review before send\nsecond line"
            before = list(factory.sockets[-1].sent_bytes)
            await app.set_props_and_fire("agent-tools-toggle", {}, "click")
            await app.set_props_and_fire("tools-paste", {}, "click")
            assert _has(app, "paste-review-confirm") and session.pending_review is not None
            assert factory.sockets[-1].sent_bytes == before
            await app.set_props_and_fire("paste-review-cancel", {}, "click")
            await app.set_props_and_fire("agent-tools-toggle", {}, "click")
            opening = asyncio.create_task(app.set_props_and_fire("tools-select-text", {}, "click"))
            await _wait_for(lambda: len(_snapshot_requests(sent)) == 1)
            (request,) = struct.unpack_from(">I", _snapshot_requests(sent)[0], 1)
            term._test_inject_dart_message(protocol.encode_snapshot_chunk(request, 6, 0, b"screen"))
            await opening
            assert app.find("select-text-field").value == "screen"
            await app.set_props_and_fire("select-text-close", {}, "click")
            assert session.current_term is term
            assert _signature(_path_to(app, "terminal-surface")) == path
            assert session.store.draft == "unsent draft"
        finally:
            await session.dispose()

    run(scenario())


def test_split_tools_are_absent_without_control() -> None:
    async def scenario() -> None:
        session, _ops, _factory, _submitted = _agent_session()
        session.open()
        app = await render_component(
            mount_agent_screen,
            session,
            width_tier="expanded",
            typing_split=True,
            touch=True,
        )
        try:
            assert not _has(app, "agent-tools-toggle")
            assert not _has(app, "tools-popover")
            assert not _has(app, "agent-split-dock")
        finally:
            await session.dispose()

    run(scenario())


@ft.component
def _VisibleSplit(session: Any) -> Any:
    height, set_height = ft.use_state(900)
    return ft.Column(
        expand=True,
        controls=[
            ft.Button("Keyboard", key="viewport-keyboard", on_click=lambda _: set_height(396)),
            ft.Container(
                expand=True,
                content=mount_agent_screen(
                    session,
                    width_tier="expanded",
                    typing_split=True,
                    touch=True,
                    viewport_height=height,
                    keyboard_up=height < 900,
                ),
            ),
        ],
    )


def test_split_tools_follow_the_visible_viewport_before_layout_catches_up() -> None:
    async def scenario() -> None:
        session, _ops, _factory, _submitted = _agent_session()
        session.open()
        await session.confirm_takeover()
        app = await render_component(_VisibleSplit, session)
        try:
            await app.set_props_and_fire(
                "agent-detail-panel", {}, "size_change", {"w": 1376, "h": 900}
            )
            await app.set_props_and_fire(
                "terminal-flex-host", {}, "size_change", {"w": 900, "h": 820}
            )
            terminal = app.find("terminal-surface")._i
            composer = app.find("composer-draft")._i
            await app.set_props_and_fire("agent-tools-toggle", {}, "click")
            before = app.find("tools-popover-panel")
            bounds = (before.top, before.bottom)
            await app.set_props_and_fire("viewport-keyboard", {}, "click")
            app.session.apply_patch(app.page._i, {"width": 1376, "height": 396})
            panel = app.find("tools-popover-panel")
            assert app.find("tools-popover").visible
            assert panel.height is None and (panel.top, panel.bottom) == bounds
            assert 0 < 396 - panel.top - panel.bottom <= 316
            await app.set_props_and_fire(
                "agent-detail-panel", {}, "size_change", {"w": 1376, "h": 396}
            )
            await app.set_props_and_fire(
                "terminal-flex-host", {}, "size_change", {"w": 900, "h": 316}
            )
            panel = app.find("tools-popover-panel")
            assert 0 < 396 - panel.top - panel.bottom <= 316
            assert app.find("terminal-surface")._i == terminal
            assert app.find("composer-draft")._i == composer
            assert app.find("composer-draft").can_request_focus
            await app.set_props_and_fire("tools-popover-scrim", {}, "tap")
            assert not app.find("tools-popover").visible
        finally:
            await session.dispose()

    run(scenario())
