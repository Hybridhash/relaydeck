"""Renderers and sockets for switch-gap tests."""

from __future__ import annotations

from typing import Any
from urllib.parse import urlencode

from tests.support.asgi_socket import AsgiSocket
from tests.support.terminal.reader_handoff import _Socket


class _TicketOnlySocket(_Socket):
    def __init__(self, factory: Any, ticket: str) -> None:
        self.factory, self.ticket = factory, ticket
        self.socket: AsgiSocket | None = None
        self.sent: list[bytes] = []

    async def connect(self) -> None:
        controller = self.factory.actor.controller
        query = urlencode({"ticket": self.ticket})
        self.socket = AsgiSocket(self.factory.app, f"/v1/terminal/{controller.lease_id}?{query}")
        await self.socket.open()


class _Renderer:
    def __init__(self) -> None:
        self.output: list[bytes] = []
        self.resets = 0

    def reset(self) -> None:
        self.resets += 1
        self.output.clear()

    def set_input_enabled(self, _: bool) -> None:
        pass


def _write(term: _Renderer, data: bytes) -> bool:
    term.output.append(data)
    return True
