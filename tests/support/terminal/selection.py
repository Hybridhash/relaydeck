"""Selection test helpers."""

from __future__ import annotations

import asyncio
import struct
from typing import Any

from relaydeck_terminal import protocol

from relaydeck.client.features.terminal.agent_session import AgentSession
from relaydeck.client.features.terminal.screen import SELECT_BUTTON_KEY
from tests.support.host_harness import HostApp
from tests.support.terminal.binding import _wait_for


def _mark_ready(session: AgentSession) -> tuple[Any, list[bytes]]:
    term = session.current_term
    assert term is not None
    sent: list[bytes] = []
    term._test_mark_ready(sent)
    return term, sent


def _snapshot_requests(sent: list[bytes]) -> list[bytes]:
    # The mount effect also flushes an INPUT_ENABLED packet; only 0x09
    # packets are snapshot requests.
    return [packet for packet in sent if packet[:1] == b"\x09"]


async def _open_with_snapshot(app: HostApp, term: Any, sent: list[bytes], body: str) -> None:
    opener = asyncio.create_task(app.set_props_and_fire(SELECT_BUTTON_KEY, {}, "click"))
    try:
        await _wait_for(lambda: len(_snapshot_requests(sent)) == 1)
        (req_id,) = struct.unpack_from(">I", _snapshot_requests(sent)[0], 1)
        payload = body.encode()
        term._test_inject_dart_message(
            protocol.encode_snapshot_chunk(req_id, len(payload), 0, payload)
        )
        await opener
    finally:
        if not opener.done():
            opener.cancel()
