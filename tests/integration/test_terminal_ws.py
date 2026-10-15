"""terminal client against a real WebSocket server."""

from __future__ import annotations

import asyncio
import json
from typing import Any

import websockets.asyncio.server as ws_server

from relaydeck.client.features.sync.controller import TerminalStreamClient
from relaydeck.client.features.sync.transport import WebsocketTerminalSocket
from relaydeck.gateway import terminal_wire


def test_client_against_live_server() -> None:
    async def scenario() -> None:
        received: list[tuple[str, Any]] = []

        async def handler(connection: Any) -> None:
            await connection.send(terminal_wire.encode_output("e7", 0, b"boot\r\n"))
            await connection.send(terminal_wire.encode_output("e7", 6, b"$ "))
            offset = 8
            async for message in connection:
                if isinstance(message, bytes):
                    chunk = terminal_wire.decode_input(message)
                    received.append(
                        ("bytes", (chunk.generation, chunk.input_seq, chunk.kind, chunk.data))
                    )
                    if chunk.kind == "resize":
                        continue
                    echo = b"echo:" + chunk.data if chunk.kind == "text" else chunk.data
                    await connection.send(terminal_wire.encode_output("e7", offset, echo))
                    offset += len(echo)
                    await connection.send(json.dumps({"kind": "bye"}))
                else:
                    doc = json.loads(message)
                    received.append(("text", doc))
                    if doc.get("kind") == "bye":
                        return

        server = await ws_server.serve(handler, "127.0.0.1", 0)
        port = server.sockets[0].getsockname()[1]
        try:
            outputs: list[tuple[int, bytes]] = []
            gaps: list[str] = []
            controls: list[dict[str, Any]] = []
            client = TerminalStreamClient(
                socket_factory=lambda ticket: WebsocketTerminalSocket(
                    f"ws://127.0.0.1:{port}/v1/terminal/lease-9?ticket={ticket}"
                ),
                on_output=lambda seq, data: outputs.append((seq, data)),
                on_control=controls.append,
                on_gap=gaps.append,
            )
            await client.open("lease-9", 7, "rdt_live")
            await client.send_input(b"ls\n")
            await client.resize(100, 30)
            await asyncio.wait_for(client.run(), timeout=10)
        finally:
            server.close()
            await server.wait_closed()
        assert outputs == [(0, b"boot\r\n"), (6, b"$ "), (8, b"echo:ls\n")]
        assert gaps == []
        assert ("bytes", (7, 1, "text", b"ls\n")) in received
        resizes = [r for r in received if r[0] == "bytes" and r[1][2] == "resize"]
        assert len(resizes) == 1
        assert (
            terminal_wire.decode_input(terminal_wire.encode_resize("lease-9", 7, 2, 100, 30)).cols
            == 100
        )

    asyncio.run(scenario())
