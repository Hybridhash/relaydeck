"""RELAY-OVERFLOW: one terminal's output burst is a per-terminal condition. It never stops the
link, it never takes another terminal down, that terminal resyncs, and a burst of resize events
replays the screen once. Synthetic bytes; no terminal content is asserted or logged."""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import Any

import pytest

from relaydeck.connector.attach import AttachSession
from relaydeck.connector.herdr import HerdrAdapter, HerdrProtocolError
from relaydeck.connector.link import ConnectorLink, LinkConfig
from relaydeck.connector.source import (
    MAX_RELAY_BYTES,
    RESIZE_SETTLE_S,
    TUNNEL_MAX_OUTPUT,
    HerdrSource,
)


def _source(tmp_path: Path) -> HerdrSource:
    return HerdrSource(HerdrAdapter(socket_path=str(tmp_path / "unused.sock")))


def test_an_overflow_drops_only_that_terminal_and_asks_for_a_resync(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    source = _source(tmp_path)
    source.script_output("quiet", "e-q", b"q" * 1000)
    chunk = b"x" * (MAX_RELAY_BYTES // 2)
    with caplog.at_level(logging.WARNING):
        source.script_output("loud", "e-l", chunk)
        assert source.script_output("loud", "e-l", chunk + b"y" * 2000) == 0  # over the bound
        assert source.script_output("loud", "e-l", b"z") == 0  # held until the resync is asked
    lines = [r.getMessage() for r in caplog.records if "overflow" in r.getMessage()]
    assert len(lines) == 1 and "terminal=loud" in lines[0]
    # The quiet terminal's bytes are still there and still deliver; the loud one's are gone.
    delivered = source.drain_outputs()
    assert [(t, e, len(d)) for t, e, d in delivered] == [("quiet", "e-q", 1000)]
    assert source.drain_terminal_resets() == {"loud": "e-l"}
    # After the reset went out, that terminal stages again (old-epoch frames are fenced upstream).
    assert source.script_output("loud", "e-l", b"fresh") == 1


def test_the_link_does_not_fail_the_source_for_an_output_overflow(tmp_path: Path) -> None:
    async def scenario() -> None:
        link = ConnectorLink(
            LinkConfig(
                "http://127.0.0.1:9", "h1", "synthetic", str(tmp_path / "c.db"), max_retries=3
            ),
            _source(tmp_path),
        )
        calls = 0

        async def session(_stop: asyncio.Event) -> str:
            nonlocal calls
            calls += 1
            if calls == 1:
                raise HerdrProtocolError("output_overflow", "terminal relay capacity exceeded")
            return "stop"

        async def no_sleep(_seconds: float) -> None:
            return None

        link._session = session  # type: ignore[method-assign]
        link._sleep = no_sleep  # type: ignore[assignment]
        outcome = await link.run(asyncio.Event())
        assert outcome.reason == "shutdown" and calls == 2  # reconnected, never source-failed

    asyncio.run(scenario())


def test_a_real_source_failure_still_stops_the_link(tmp_path: Path) -> None:
    async def scenario() -> None:
        link = ConnectorLink(
            LinkConfig("http://127.0.0.1:9", "h1", "synthetic", str(tmp_path / "c.db")),
            _source(tmp_path),
        )

        async def session(_stop: asyncio.Event) -> str:
            raise HerdrProtocolError("bad_snapshot", "invalid")

        link._session = session  # type: ignore[method-assign]
        assert (await link.run(asyncio.Event())).reason == "source-failed"

    asyncio.run(scenario())


class _Resizable(AttachSession):
    def __init__(self) -> None:
        super().__init__(["synthetic"])
        self.sizes: list[tuple[int, int]] = []
        self.cols, self.rows = 80, 24

    @property
    def running(self) -> bool:
        return True

    def resize(self, cols: int, rows: int) -> None:
        self.sizes.append((cols, rows))
        self.cols, self.rows = cols, rows


def _attached(source: HerdrSource, session: AttachSession) -> None:
    source._attached["agent:t1"] = (session, "t1", "")
    source._epochs = {"t1": "e1"}
    source._epoch_kinds = {"t1": "agent"}
    source._epoch_modes = {"t1": "control"}


def test_twenty_rapid_resizes_apply_once(tmp_path: Path) -> None:
    async def scenario() -> None:
        source = _source(tmp_path)
        session = _Resizable()
        _attached(source, session)
        for index in range(20):
            rows = 43 if index % 2 == 0 else 54  # the iPhone's keyboard flip
            assert source.resize_terminal("t1", "e1", 100, rows) is None
        assert session.sizes == []  # nothing yet: the burst is still settling
        await asyncio.sleep(RESIZE_SETTLE_S + 0.1)
        assert session.sizes == [(100, 54)]  # the latest size, once
        # The same size again is still applied: a reattached viewer relies on that repaint.
        source.resize_terminal("t1", "e1", 100, 54)
        await asyncio.sleep(RESIZE_SETTLE_S + 0.1)
        assert session.sizes == [(100, 54), (100, 54)]
        # A stale epoch is refused at once.
        assert source.resize_terminal("t1", "old", 90, 30) == "stale or unavailable terminal"

    asyncio.run(scenario())


def test_dropping_a_terminal_cancels_its_pending_resize(tmp_path: Path) -> None:
    async def scenario() -> None:
        source = _source(tmp_path)
        session = _Resizable()
        _attached(source, session)
        source.resize_terminal("t1", "e1", 120, 40)
        source.drop_terminal("t1")
        await asyncio.sleep(RESIZE_SETTLE_S + 0.1)
        assert session.sizes == []

    asyncio.run(scenario())


def test_the_pump_chunk_never_exceeds_the_tunnel_bound() -> None:
    assert TUNNEL_MAX_OUTPUT <= MAX_RELAY_BYTES // 8  # a full read always fits many times over


def test_relay_signature_is_unchanged_for_other_callers() -> None:
    # `script` still returns frames staged for the normal case.
    from relaydeck.connector.source import TerminalRelay

    relay = TerminalRelay()
    assert relay.script("t", "e", b"a" * (TUNNEL_MAX_OUTPUT + 1)) == 2


def test_overflow_on_one_terminal_resyncs_it_while_the_other_keeps_delivering(
    tmp_path: Path,
) -> None:
    """Through the link's own output path: the loud terminal gets one reset_required (the
    gateway's existing per-terminal resync), the quiet one's bytes are delivered, and nothing
    is raised."""
    import base64
    import json

    async def scenario() -> None:
        source = _source(tmp_path)
        link = ConnectorLink(
            LinkConfig("http://127.0.0.1:9", "h1", "synthetic", str(tmp_path / "c.db")), source
        )
        link._last_epochs = {"quiet": "e-q", "loud": "e-l"}
        sent: list[dict[str, Any]] = []

        class Transport:
            async def send_text(self, text: str) -> None:
                sent.append(json.loads(text))

        source.script_output("quiet", "e-q", b"hello")
        source.script_output("loud", "e-l", b"a" * (MAX_RELAY_BYTES // 2))
        assert source.script_output("loud", "e-l", b"b" * MAX_RELAY_BYTES) == 0
        try:
            await link._push_terminal_updates(Transport())  # type: ignore[arg-type]
        finally:
            link._journal.close()
        kinds = [(doc["kind"], doc.get("terminal_id")) for doc in sent]
        assert ("terminal.reset_required", "loud") in kinds
        outputs = [doc for doc in sent if doc["kind"] == "terminal.output"]
        assert [base64.b64decode(doc["data_b64"]) for doc in outputs] == [b"hello"]
        assert all(doc["terminal_id"] == "quiet" for doc in outputs)
        assert source.relay.load == 0 and not source.relay.overflowed

    asyncio.run(scenario())
