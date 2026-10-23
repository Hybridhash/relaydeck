"""Direct-typing event helpers."""

from __future__ import annotations

from typing import Any

from flet.messaging.protocol import MessageAction

from relaydeck.client.features.shell.screen import mount_shell_screen
from relaydeck.client.features.terminal.surfaces import mount_agent_screen
from relaydeck.client.features.terminal.switcher import SwitcherRow
from tests.support.host_harness import render_component
from tests.support.terminal.sessions import _agent_session, _session


async def _direct(shell: bool, tier: str = "compact") -> tuple[Any, Any, Any, list[int]]:
    if shell:
        session, _ops, factory = _session()
        await session.open("term-1")
        app = await render_component(
            mount_shell_screen,
            session,
            phone=True,
            viewport_height=844,
            width_tier=tier,
            on_back=lambda: None,
            on_new_terminal=lambda: None,
        )
    else:
        session, _ops, factory, _submitted = _agent_session()
        session.open()
        await session.confirm_takeover()
        session.on_draft_change("retained compose draft")
        app = await render_component(
            mount_agent_screen,
            session,
            phone=True,
            viewport_height=844,
            width_tier=tier,
            switcher_rows=(SwitcherRow("home-mac:term-1", "term-1", "", "idle", True),),
            on_agent_tab=lambda key: None,
        )
    app.session.apply_patch(
        app.page._i, {"width": 390 if tier == "compact" else 768, "height": 844}
    )
    focuses: list[int] = []
    send = app.conn.send_message

    def acknowledge(message: Any) -> None:
        send(message)
        if message.action == MessageAction.INVOKE_METHOD and message.body.name == "focus":
            body = message.body
            focuses.append(body.control_id)
            app.session.handle_invoke_method_results(body.control_id, body.call_id, None, None)

    app.conn.send_message = acknowledge
    if shell:
        await app.set_props_and_fire("shell-keyboard-toggle", {}, "click")
    else:
        await open_direct_input(app)
    return app, session, factory.sockets[0], focuses


async def open_direct_input(app: Any) -> None:
    """Agent phone screen: tools button, then the Direct mode segment."""
    await app.set_props_and_fire("terminal-tools-button", {}, "click")
    await app.set_props_and_fire("tools-mode-direct", {}, "click")


async def open_compose(app: Any) -> None:
    """Agent phone screen: tools button, then the Compose mode segment."""
    await app.set_props_and_fire("terminal-tools-button", {}, "click")
    await app.set_props_and_fire("tools-mode-compose", {}, "click")


def _typing_key(app: Any) -> str:
    for key in ("agent-workflow-direct-typing", "shell-mode-text-typing"):
        try:
            app.find(key)
            return key
        except KeyError:
            pass
    raise AssertionError("Direct field missing")


async def _ack_reset(app: Any) -> None:
    if not (app.find(_typing_key(app)).data or {}).get("direct_reset"):
        return
    await app.set_props_and_fire(
        _typing_key(app),
        {},
        "selection_change",
        {"selected_text": "", "selection": {"base_offset": 2, "extent_offset": 2}},
    )


async def _change(app: Any, value: str, *, ack: bool = True) -> None:
    await app.set_props_and_fire(_typing_key(app), {"value": value}, "change", value)
    if ack:
        await _ack_reset(app)
