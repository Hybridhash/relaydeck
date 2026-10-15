"""source bootstrap over a scripted Unix socket.

Subscribe-ack-snapshot-buffer convergence against a fake daemon that
speaks real NDJSON over real asyncio streams, so fragmented writes,
id mismatch, EOF restart and ack rejection exercise the true
transport path. No writes go to the user's daemon.
"""

from __future__ import annotations

import asyncio
import itertools
import json
import os
from pathlib import Path
from typing import Any

import pytest

from relaydeck.connector.herdr import (
    HerdrAdapter,
    HerdrError,
    HerdrProtocolError,
)


def _line(doc: dict[str, Any]) -> bytes:
    return (json.dumps(doc) + "\n").encode()


_sock_counter = itertools.count()


def _sock_path() -> Path:
    # AF_UNIX paths are capped at 104 chars on macOS; pytest tmp
    # paths are far longer, so bind a short unique /tmp name.
    return Path(f"/tmp/rd-boot-{os.getpid()}-{next(_sock_counter)}.sock")


class _Daemon:
    """Scripted Herdr daemon on a Unix socket in tmp_path."""

    def __init__(
        self,
        *,
        protocol: int = 22,
        ack_type: str = "subscription_started",
        ack_error: dict[str, str] | None = None,
        ack_id: str | None = None,
        snapshot_delay: float = 0.0,
        sub_events: list[bytes] | None = None,
        fragment: int = 0,
        close_sub_after_ack: bool = False,
    ) -> None:
        self.protocol = protocol
        self.ack_type = ack_type
        self.ack_error = ack_error
        self.ack_id = ack_id
        self.snapshot_delay = snapshot_delay
        self.sub_events = sub_events or []
        self.fragment = fragment
        self.close_sub_after_ack = close_sub_after_ack
        self.methods: list[str] = []
        self._server: asyncio.AbstractServer | None = None

    async def start(self, sock: Path) -> _Daemon:
        self._server = await asyncio.start_unix_server(self._handle, str(sock))
        return self

    async def stop(self) -> None:
        assert self._server is not None
        self._server.close()
        await self._server.wait_closed()

    async def _write(self, writer: asyncio.StreamWriter, payload: bytes) -> None:
        if self.fragment <= 0:
            writer.write(payload)
            await writer.drain()
            return
        for i in range(0, len(payload), self.fragment):
            writer.write(payload[i : i + self.fragment])
            await writer.drain()
            await asyncio.sleep(0)

    async def _handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            raw = await reader.readline()
            if not raw:
                return
            doc = json.loads(raw)
            method = str(doc.get("method", ""))
            rid = str(doc.get("id", ""))
            self.methods.append(method)
            if method == "ping":
                writer.write(_line({"id": rid, "result": {"protocol": self.protocol}}))
                await writer.drain()
                return
            if method == "session.snapshot":
                if self.snapshot_delay:
                    await asyncio.sleep(self.snapshot_delay)
                writer.write(_line({"id": rid, "result": {"tabs": [], "epoch": 7}}))
                await writer.drain()
                return
            if method == "agent.list":
                writer.write(_line({"id": rid, "result": {"agents": [{"id": "a1"}]}}))
                await writer.drain()
                return
            if method == "events.subscribe":
                if self.ack_error is not None:
                    writer.write(_line({"id": rid, "error": dict(self.ack_error)}))
                    await writer.drain()
                    return
                writer.write(_line({"id": self.ack_id or rid, "result": {"type": self.ack_type}}))
                await writer.drain()
                if self.close_sub_after_ack:
                    return
                for event in self.sub_events:
                    await self._write(writer, event)
                # Hold the subscription open until the client goes away.
                while True:
                    chunk = await reader.read(1024)
                    if not chunk:
                        return
        finally:
            writer.close()

    def adapter(self, sock: Path) -> HerdrAdapter:
        return HerdrAdapter(socket_path=str(sock), timeout=10.0)


def test_bootstrap_converges_with_buffered_prefix() -> None:
    async def scenario() -> None:
        sock = _sock_path()
        events = [
            _line({"event": "agent.started", "data": {"id": "a1"}}),
            _line({"event": "pane.exited", "data": {"pane_id": "p9"}}),
        ]
        daemon = await _Daemon(snapshot_delay=0.2, sub_events=events, fragment=7).start(sock)
        try:
            adapter = daemon.adapter(sock)
            result = await adapter.bootstrap([{"kinds": ["*"]}])
            try:
                assert result.snapshot == {"tabs": [], "epoch": 7}
                assert result.agents == [{"id": "a1"}]
                assert result.buffered == [
                    ("agent.started", {"id": "a1"}),
                    ("pane.exited", {"pane_id": "p9"}),
                ]
                # Subscribe preceded the snapshot reads: nothing was missed.
                assert daemon.methods[0] == "ping"
                assert daemon.methods[1] == "events.subscribe"
                assert set(daemon.methods[2:]) == {"session.snapshot", "agent.list"}
            finally:
                await result.subscription.aclose()
        finally:
            await daemon.stop()

    asyncio.run(scenario())


def test_bootstrap_live_stream_handed_back() -> None:
    async def scenario() -> None:
        sock = _sock_path()
        daemon = await _Daemon(
            sub_events=[_line({"event": "e1", "data": {}})],
        ).start(sock)
        try:
            adapter = daemon.adapter(sock)
            result = await adapter.bootstrap([])
            try:
                assert result.buffered == [("e1", {})]
            finally:
                await result.subscription.aclose()
        finally:
            await daemon.stop()

    asyncio.run(scenario())


def test_bootstrap_rejects_wrong_ack_type() -> None:
    async def scenario() -> None:
        sock = _sock_path()
        daemon = await _Daemon(ack_type="nope").start(sock)
        try:
            adapter = daemon.adapter(sock)
            with pytest.raises(HerdrProtocolError):
                await adapter.bootstrap([])
        finally:
            await daemon.stop()

    asyncio.run(scenario())


def test_bootstrap_rejects_ack_id_mismatch() -> None:
    async def scenario() -> None:
        sock = _sock_path()
        daemon = await _Daemon(ack_id="someone-else").start(sock)
        try:
            adapter = daemon.adapter(sock)
            with pytest.raises(HerdrProtocolError):
                await adapter.bootstrap([])
        finally:
            await daemon.stop()

    asyncio.run(scenario())


def test_bootstrap_fails_closed_on_protocol_drift() -> None:
    async def scenario() -> None:
        sock = _sock_path()
        daemon = await _Daemon(protocol=21).start(sock)
        try:
            adapter = daemon.adapter(sock)
            with pytest.raises(HerdrProtocolError) as exc:
                await adapter.bootstrap([])
            assert exc.value.code == "protocol_mismatch"
            assert daemon.methods == ["ping"]
        finally:
            await daemon.stop()

    asyncio.run(scenario())


def test_bootstrap_restart_midway_fails_closed() -> None:
    async def scenario() -> None:
        sock = _sock_path()
        daemon = await _Daemon(snapshot_delay=0.3, close_sub_after_ack=True).start(sock)
        try:
            adapter = daemon.adapter(sock)
            with pytest.raises(HerdrProtocolError) as exc:
                await adapter.bootstrap([])
            assert exc.value.code == "upstream_closed"
        finally:
            await daemon.stop()

    asyncio.run(scenario())


def test_bootstrap_invalid_event_line_fails() -> None:
    async def scenario() -> None:
        sock = _sock_path()
        daemon = await _Daemon(snapshot_delay=0.2, sub_events=[b"not json\n"]).start(sock)
        try:
            adapter = daemon.adapter(sock)
            with pytest.raises(HerdrProtocolError) as exc:
                await adapter.bootstrap([])
            assert exc.value.code == "bad_event"
        finally:
            await daemon.stop()

    asyncio.run(scenario())


def test_invalid_pane_rejection_recovers_with_global_scope() -> None:
    async def scenario() -> None:
        sock = _sock_path()
        daemon = await _Daemon(
            ack_error={"code": "invalid_pane", "message": "gone"},
        ).start(sock)
        try:
            adapter = daemon.adapter(sock)
            with pytest.raises(HerdrError) as exc:
                await adapter.bootstrap([{"panes": ["missing"]}])
            assert exc.value.code == "invalid_pane"
            # Recover the global baseline, then rebuild pane-scoped subs.
            daemon.ack_error = None
            result = await adapter.bootstrap([{"kinds": ["*"]}])
            try:
                assert result.snapshot["epoch"] == 7
                assert result.buffered == []
            finally:
                await result.subscription.aclose()
        finally:
            await daemon.stop()

    asyncio.run(scenario())
