"""Uncommitted native text survives parking without becoming terminal input."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import flet as ft
import pytest

from relaydeck.client.features.terminal.input_modes import ROUTE_DROPPED, ROUTE_PTY
from relaydeck.client.features.terminal.surfaces.direct_typing import DirectTypingField
from tests.support.host_harness import render_component, run
from tests.support.terminal.direct_typing import _change, _direct, _typing_key
from tests.support.terminal.latch_text import _inputs


async def _editor() -> tuple[Any, dict[str, Any], list[bytes]]:
    actions: dict[str, Any] = {}
    sent: list[bytes] = []
    field = ft.Ref[ft.TextField]()

    async def send(_origin: int, data: bytes) -> str:
        if actions.get("drop"):
            return ROUTE_DROPPED
        sent.append(data)
        return ROUTE_PTY

    @ft.component
    def Fixture() -> Any:
        mounted, actions["mount"] = ft.use_state(True)
        visible, actions["visible"] = ft.use_state(True)
        binding, actions["binding"] = ft.use_state(1)
        requests, actions["reset"] = ft.use_state(0)
        return ft.Column(
            controls=[
                DirectTypingField(
                    key="editor-component",
                    on_input=send,
                    visible=visible,
                    enabled=True,
                    binding=binding,
                    reset_requests=requests,
                    field_ref=field,
                    focus_requests=0,
                    focus_handled=SimpleNamespace(current=0),
                    dark=True,
                    field_key="parked-editor",
                )
                if mounted
                else ft.Container(key="empty")
            ]
        )

    return await render_component(Fixture), actions, sent


@pytest.mark.parametrize("leave", ["blur", "hide", "unmount"])
@pytest.mark.parametrize("replacement", [False, True])
def test_direct_parked_editor_restore_never_sends_or_rebases(leave: str, replacement: bool) -> None:
    async def scenario() -> None:
        app, actions, sent = await _editor()
        await app.set_props_and_fire("parked-editor", {"value": "  abc"}, "change", "  abc")
        field = app.find("parked-editor")
        native = "  replacement😀" if replacement else "  abc😀"
        app.session.apply_patch(
            field._i,
            {"value": native, "selection": {"base_offset": 3, "extent_offset": 4}},
        )
        if leave == "blur":
            await app.set_props_and_fire("parked-editor", {}, "blur")
        else:
            actions["visible" if leave == "hide" else "mount"](False)
            await app.settle()
            actions["visible" if leave == "hide" else "mount"](True)
            await app.settle()
        field = app.find("parked-editor")
        assert field.value == native
        assert field.selection.base_offset == 3 and field.selection.extent_offset == 4
        assert not field.data["direct_reset"] and sent == [b"abc"]
        for _ in range(2):
            await app.set_props_and_fire(
                "parked-editor", {"value": native + "!"}, "change", native + "!"
            )
        assert sent == [b"abc"] + (
            [b"\x7f"] * 3 + ["replacement😀!".encode()] if replacement else ["😀!".encode()]
        )

    run(scenario())


@pytest.mark.parametrize("boundary", ["binding", "reset", "drop"])
def test_direct_parked_editor_hard_boundary_discards_unsent_text(boundary: str) -> None:
    async def scenario() -> None:
        app, actions, sent = await _editor()
        await app.set_props_and_fire("parked-editor", {"value": "  abc"}, "change", "  abc")
        app.session.apply_patch(app.find("parked-editor")._i, {"value": "  abc pending"})
        await app.set_props_and_fire("parked-editor", {}, "blur")
        assert app.find("parked-editor").value == "  abc pending" and sent == [b"abc"]
        if boundary == "drop":
            actions["drop"] = True
            await app.set_props_and_fire(
                "parked-editor", {"value": "  abc pending!"}, "change", "  abc pending!"
            )
        else:
            actions[boundary](2)
            await app.settle()
        assert app.find("parked-editor").value == "  " and sent == [b"abc"]

    run(scenario())


@pytest.mark.parametrize("shell", [False, True])
@pytest.mark.parametrize("boundary", ["control", "generation", "review"])
def test_direct_native_unsent_text_cannot_survive_active_authority_loss(
    shell: bool, boundary: str
) -> None:
    async def scenario() -> None:
        app, session, socket, _ = await _direct(shell)
        try:
            await _change(app, "  abc")
            app.session.apply_patch(app.find(_typing_key(app))._i, {"value": "  abc pending"})
            if boundary == "control":
                session.controller.mode = "view"
                session._notify()
            elif boundary == "generation":
                session._bump_generation("test")
                session._notify()
            else:
                await session.request_paste("review\nthis")
            await app.settle()
            assert app.find(_typing_key(app)).value == "  "
            assert _inputs(socket) == [b"abc"]
        finally:
            await session.dispose()

    run(scenario())
