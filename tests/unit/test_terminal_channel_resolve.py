"""Session-bound data-channel lookup survives a detached retained control."""

import asyncio
import logging
import weakref

import pytest
from flet.data_channel import DataChannel, _ProtocolMuxedDataChannel
from relaydeck_terminal.terminal import RelayDeckTerminal

from tests.support.host_harness import FakeConnection


class _ChannelConnection(FakeConnection):
    def __init__(self) -> None:
        super().__init__()
        self.channels: dict[int, DataChannel] = {}

    def data_channel_for(self, channel_id: int) -> DataChannel:
        if channel_id not in self.channels:
            self.channels[channel_id] = _ProtocolMuxedDataChannel(channel_id, self)
        return self.channels[channel_id]


class _RecordedSession:
    def __init__(self, connection: _ChannelConnection | None) -> None:
        self.connection = connection


def test_resolve_channel_falls_back_to_the_recorded_sessions_current_connection() -> None:
    term = RelayDeckTerminal()
    first = _ChannelConnection()
    session = _RecordedSession(first)
    term._session_ref = weakref.ref(session)
    assert term._resolve_channel(42) is first.data_channel_for(42)
    replacement = _ChannelConnection()
    session.connection = replacement
    assert term._resolve_channel(42) is replacement.data_channel_for(42)
    assert term._resolve_channel(42) is not first.data_channel_for(42)


@pytest.mark.parametrize("change", ["connection", "channel", "terminal"])
def test_repeated_session_fallback_logs_once_until_the_route_changes(
    change: str, caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    logger = logging.getLogger("relaydeck.client.typing_split")
    monkeypatch.setattr(logger, "propagate", True)
    term = RelayDeckTerminal()
    connection = _ChannelConnection()
    session = _RecordedSession(connection)
    term._session_ref = weakref.ref(session)
    channel_id = 42
    expected = connection.data_channel_for(channel_id)
    lookup = connection.data_channel_for
    lookups: list[int] = []

    def record_lookup(number: int) -> DataChannel:
        lookups.append(number)
        return lookup(number)

    monkeypatch.setattr(connection, "data_channel_for", record_lookup)
    with caplog.at_level(logging.INFO, logger=logger.name):
        for _ in range(1000):
            assert term._resolve_channel(channel_id) is expected
        assert lookups == [channel_id] * 1000
        records = [r for r in caplog.records if r.getMessage().startswith("term_channel_resolve ")]
        assert len(records) == 1
        assert records[0].getMessage() == (
            f"term_channel_resolve via=session term={id(term) % 100000} id=42"
        )

        if change == "connection":
            session.connection = _ChannelConnection()
        elif change == "channel":
            channel_id = 43
        else:
            term = RelayDeckTerminal()
            term._session_ref = weakref.ref(session)
        expected = session.connection.data_channel_for(channel_id)
        for _ in range(1000):
            assert term._resolve_channel(channel_id) is expected

    records = [r for r in caplog.records if r.getMessage().startswith("term_channel_resolve ")]
    assert len(records) == 2
    assert records[-1].getMessage() == (
        f"term_channel_resolve via=session term={id(term) % 100000} id={channel_id}"
    )


@pytest.mark.parametrize("reason", ["unrecorded", "disconnected", "collected"])
def test_resolve_channel_reraises_when_no_recorded_session_connection_exists(reason: str) -> None:
    term = RelayDeckTerminal()
    if reason != "unrecorded":
        session = _RecordedSession(None)
        term._session_ref = weakref.ref(session)
        if reason == "collected":
            del session
    with pytest.raises(RuntimeError, match="Control must be added to the page first"):
        term._resolve_channel(42)


def test_rebind_transport_logs_a_failed_resolution(
    caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(logging.getLogger("relaydeck.client.typing_split"), "propagate", True)

    async def scenario() -> None:
        term = RelayDeckTerminal()
        term._channel_id = 42
        with caplog.at_level(logging.WARNING, logger="relaydeck.client.typing_split"):
            assert term.rebind_transport() is False
        record = caplog.records[-1]
        assert record.getMessage() == f"term_rebind_failed term={id(term) % 100000} id=42"
        assert record.exc_info is not None and record.exc_info[0] is RuntimeError

    asyncio.run(scenario())
