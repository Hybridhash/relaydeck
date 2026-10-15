"""Secret-free connector close causes, using pinned websockets 16 events."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import pytest
from websockets.client import ClientProtocol
from websockets.datastructures import Headers
from websockets.exceptions import ConnectionClosedError
from websockets.frames import Close, Frame, Opcode
from websockets.http11 import Response
from websockets.uri import parse_uri

from relaydeck.connector import link as link_module
from relaydeck.connector.fixture import FixtureAdapter
from relaydeck.connector.link import (
    ConnectorLink,
    LinkConfig,
    _PongTrackedConnection,
    _WebsocketTransport,
)
from tests.support.sync.scripted_transport import _ScriptedTransport


def test_real_wire_pong_updates_monotonic_age_and_keeps_builtin_acknowledgement() -> None:
    async def scenario() -> None:
        connection = _PongTrackedConnection(ClientProtocol(parse_uri("ws://localhost/test")))
        connection.response = Response(101, "Switching Protocols", Headers())
        assert connection.ping_interval == connection.ping_timeout == 20
        assert connection.last_pong_at is None
        connection.process_event(Frame(Opcode.PONG, b"unsolicited"))
        assert connection.last_pong_at is None
        future: asyncio.Future[float] = asyncio.get_running_loop().create_future()
        connection.pending_pings[b"synthetic"] = (future, connection.loop.time() - 0.1)
        connection.process_event(Frame(Opcode.PONG, b"synthetic"))
        assert future.done() and future.result() >= 0.1
        assert not connection.pending_pings
        assert 0 <= connection.loop.time() - connection.last_pong_at < 0.1

    asyncio.run(scenario())


def test_transport_uses_supported_custom_connection_factory(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def scenario() -> None:
        recorded: dict[str, Any] = {}

        async def connect(url: str, **kwargs: Any) -> Any:
            recorded.update(kwargs)
            return object()

        monkeypatch.setattr("websockets.asyncio.client.connect", connect)
        transport = _WebsocketTransport("ws://localhost/v1/connectors", "synthetic")
        await transport.open()
        assert recorded["create_connection"] is _PongTrackedConnection
        assert "ping_interval" not in recorded and "ping_timeout" not in recorded

    asyncio.run(scenario())


@pytest.mark.parametrize("keepalive", [False, True])
def test_session_close_classifies_cause_and_never_logs_exception_or_reason_secrets(
    tmp_path: Path, caplog: pytest.LogCaptureFixture, keepalive: bool
) -> None:
    async def scenario() -> None:
        marker = "PRIVATE_BEARER_COOKIE_GRANT_PATH"
        exception = ConnectionClosedError(
            None, Close(1011, "keepalive ping timeout" if keepalive else marker)
        )
        exception.__cause__ = TimeoutError(marker) if keepalive else OSError(marker)
        caplog.set_level("WARNING", logger="relaydeck.connector.link")

        class Dropped(_WebsocketTransport):
            def __init__(self) -> None:
                super().__init__("ws://localhost/v1/connectors", marker)
                connection = _PongTrackedConnection(ClientProtocol(parse_uri("ws://localhost")))
                connection.last_pong_at = connection.loop.time() - 7
                self._ws = connection
                self.inbound = [
                    json.dumps({"kind": "hello", "host_id": "home-mac"}),
                    json.dumps({"kind": "pong"}),
                ]
                self.closed = False

            async def recv_text(self) -> str:
                if self.inbound:
                    return self.inbound.pop(0)
                raise exception

            async def send_text(self, text: str) -> None:
                pass

            async def close(self) -> None:
                self.closed = True

        transport = Dropped()

        async def opener() -> Dropped:
            return transport

        link = ConnectorLink(
            LinkConfig(
                gateway_url="http://localhost",
                host_id="home-mac",
                host_token=marker,
                journal_path=str(tmp_path / "journal.db"),
                max_retries=0,
                ping_interval_s=0,
            ),
            FixtureAdapter(),
            opener=opener,
        )
        assert (await link.run(asyncio.Event())).reason == "retries-exhausted"
        assert transport.closed
        assert marker not in caplog.text
        assert "exception=ConnectionClosedError code=None sent_code=1011" in caplog.text
        assert ("cause=keepalive_timeout" if keepalive else "cause=OSError") in caplog.text
        assert "last_pong_age_s=7." in caplog.text
        assert "last_app_pong_age_s=0." in caplog.text
        assert all(record.exc_info is None for record in caplog.records)

    asyncio.run(scenario())


def test_send_timeout_is_distinct_from_abnormal_wire_close(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(link_module, "SEND_TIMEOUT_S", 0.001)

    async def scenario() -> None:
        caplog.set_level("WARNING", logger="relaydeck.connector.link")

        class Blocked(_ScriptedTransport):
            async def send_text(self, text: str) -> None:
                await asyncio.Future()

        transport = Blocked([json.dumps({"kind": "hello", "host_id": "home-mac"})])

        async def opener() -> Blocked:
            return transport

        link = ConnectorLink(
            LinkConfig(
                gateway_url="http://localhost",
                host_id="home-mac",
                host_token="synthetic",
                journal_path=str(tmp_path / "journal.db"),
                max_retries=0,
            ),
            FixtureAdapter(),
            opener=opener,
        )
        assert (await link.run(asyncio.Event())).reason == "retries-exhausted"
        assert transport.closed
        assert "stage=send_timeout exception=TimeoutError" in caplog.text
        assert "last_pong_age_s=None" in caplog.text

    asyncio.run(scenario())
