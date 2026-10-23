"""A retired field's delayed focus reply must not end Flet's real receive loop."""

from __future__ import annotations

import asyncio
from typing import Any

import msgpack
import pytest
from flet.messaging.protocol import MessageAction
from flet_web.fastapi.flet_app import FletApp

from relaydeck.client.flet_invokes import install_invoke_result_guard
from tests.support.host_harness import FakeConnection, run
from tests.support.terminal.direct_typing import _change, _direct, _typing_key
from tests.support.terminal.latch_text import _inputs


class _Incoming:
    def __init__(self) -> None:
        self.frames: asyncio.Queue[bytes] = asyncio.Queue()

    async def receive_bytes(self) -> bytes:
        return await self.frames.get()

    def put(self, action: MessageAction, body: dict[str, Any]) -> None:
        self.frames.put_nowait(b"\x00" + msgpack.packb([action.value, body], use_bin_type=True))


@pytest.mark.parametrize("shell", [False, True])
def test_direct_retired_focus_reply_keeps_receive_loop_and_next_input_alive(shell: bool) -> None:
    async def scenario() -> None:
        app, session, socket, _ = await _direct(shell)
        install_invoke_result_guard(app.session)
        pending: list[Any] = []
        receive: asyncio.Task[Any] | None = None

        def hold_focus(message: Any) -> None:
            FakeConnection.send_message(app.conn, message)
            if message.action == MessageAction.INVOKE_METHOD and message.body.name == "focus":
                pending.append(message.body)

        app.conn.send_message = hold_focus
        try:
            await app.set_props_and_fire(_typing_key(app), {}, "focus")
            await _change(app, "  \n")
            native = app.find(_typing_key(app))
            request = next(req for req in reversed(pending) if req.control_id == native._i)
            assert request.name == "focus"
            await app.set_props_and_fire(_typing_key(app), {}, "focus")
            await _change(app, "  \n")
            assert request.control_id not in app.session.index

            incoming = _Incoming()
            carrier = FletApp(
                asyncio.get_running_loop(), app.conn.executor, lambda page: None, None
            )
            # Feed replies through the carrier's msgpack parser and receive loop.
            carrier._FletApp__session = app.session
            carrier._FletApp__websocket = incoming
            carrier._FletApp__send_queue = asyncio.Queue()
            receive = asyncio.create_task(carrier._FletApp__receive_loop())
            incoming.put(
                MessageAction.INVOKE_METHOD,
                {
                    "control_id": request.control_id,
                    "call_id": request.call_id,
                    "result": None,
                    "error": None,
                },
            )
            current = app.find(_typing_key(app))
            incoming.put(
                MessageAction.UPDATE_CONTROL_PROPS,
                {"id": current._i, "props": {"value": "  next"}},
            )
            incoming.put(
                MessageAction.CONTROL_EVENT,
                {"target": current._i, "name": "change", "data": "  next"},
            )
            async with asyncio.timeout(2):
                while not receive.done() and b"next" not in _inputs(socket):
                    await asyncio.sleep(0.01)
            assert not receive.done(), "orphan reply ended the Flet receive loop"
            assert _inputs(socket) == [b"\r", b"\r", b"next"]
            assert app.find(_typing_key(app))._i == current._i
            if not shell:
                assert session.store.draft == "retained compose draft"
        finally:
            if receive is not None:
                receive.cancel()
                await asyncio.gather(receive, return_exceptions=True)
            app.session.close()
            await session.dispose()

    run(scenario())
