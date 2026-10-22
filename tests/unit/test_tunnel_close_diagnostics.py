"""Secret-free connector close causes, using pinned websockets 16 events."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace
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


@pytest.mark.parametrize("branch", ["receive", "terminal"])
def test_tunnel_failure_identifies_inflight_await_without_payloads(
    branch: str, tmp_path: Path, caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def scenario() -> None:
        marker = "ticket=PRIVATE_GRANT Cookie=PRIVATE_COOKIE /private/PRIVATE_PATH"
        caplog.set_level("WARNING", logger="relaydeck.connector.link")
        inbound = (
            [json.dumps({"kind": "terminal.input", "data": marker})] if branch == "terminal" else []
        )
        transport = _ScriptedTransport(inbound)
        link = ConnectorLink(
            LinkConfig(
                gateway_url="http://localhost",
                host_id="home-mac",
                host_token=marker,
                journal_path=str(tmp_path / "journal.db"),
            ),
            FixtureAdapter(),
        )
        entered = asyncio.Event()

        async def terminal(*args: Any) -> None:
            entered.set()
            await asyncio.Event().wait()

        monkeypatch.setattr(link, "_handle_terminal", terminal)
        task = asyncio.create_task(link._serve_messages(transport, asyncio.Event()))
        try:
            if branch == "terminal":
                await asyncio.wait_for(entered.wait(), 1)
            else:
                await asyncio.sleep(0)
            link._log_tunnel_failure(transport, TimeoutError(marker), "stream")
            assert f"inflight={branch} inflight_age_s=" in caplog.text
            assert "recv_idle_s=" in caplog.text and marker not in caplog.text
            assert all(r.exc_info is None for r in caplog.records)
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            await link.close()

    asyncio.run(scenario())


def test_gateway_stopwatches_receive_handler_outbox_and_final_idle(
    caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    from fastapi.routing import APIWebSocketRoute

    from relaydeck.gateway.api import sockets as sockets_module
    from relaydeck.gateway.app import create_app
    from tests.support.terminal.sockets import _state

    async def scenario() -> None:
        caplog.set_level("INFO", logger="relaydeck.gateway.app")
        state, backend, _ = _state()
        token = state.hosts.enroll("home-mac", account="acct-1")
        endpoint = next(
            r.endpoint
            for r in create_app(state).routes
            if isinstance(r, APIWebSocketRoute) and r.path == "/v1/connectors"
        )
        now = [100.0]
        monkeypatch.setattr(sockets_module, "time", SimpleNamespace(monotonic=lambda: now[0]))
        epochs = backend.epochs_for_host
        ingest = backend.ingest_terminal_output

        def delayed_epochs(*args: Any) -> Any:
            now[0] += 0.7
            return epochs(*args)

        def delayed_output(*args: Any) -> Any:
            now[0] += 0.8
            return ingest(*args)

        monkeypatch.setattr(backend, "epochs_for_host", delayed_epochs)
        monkeypatch.setattr(backend, "ingest_terminal_output", delayed_output)
        marker = "PRIVATE_PATH_COOKIE_GRANT"

        class Socket:
            query_params = {"token": token}

            def __init__(self) -> None:
                self.inbound = [
                    json.dumps(
                        {
                            "kind": "terminal.output",
                            "host_id": "home-mac",
                            "terminal_id": marker,
                            "epoch": "e1",
                            "data_b64": "eA==",
                        }
                    ),
                    json.dumps({"kind": "bye"}),
                ]

            async def accept(self) -> None:
                pass

            async def send_text(self, data: str) -> None:
                pass

            async def close(self, **kwargs: Any) -> None:
                pass

            async def receive(self) -> dict[str, str]:
                now[0] += 0.6
                return {"type": "websocket.receive", "text": self.inbound.pop(0)}

        await endpoint(Socket())
        text = caplog.text
        for branch in ("receive", "terminal.output", "pump_outbox"):
            assert f"tunnel_stall branch={branch} ms=" in text
        assert "tunnel_loop idle_recv_s=" in text
        assert marker not in text and token not in text
        assert all(r.exc_info is None for r in caplog.records)

    asyncio.run(scenario())


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
        # The loop clock is a large float: subtracting 0.1 and measuring back can round just under.
        assert future.done() and future.result() >= 0.1 - 1e-6
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
        assert recorded["ping_interval"] == recorded["ping_timeout"] == 20

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
