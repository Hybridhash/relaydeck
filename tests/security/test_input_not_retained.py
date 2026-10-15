"""The production backend forwards accepted terminal input and keeps none of it.

Typed input can contain secrets, and a long session would otherwise grow memory without bound.
Validation, duplicate and stale rejection must behave exactly as before.
"""

import struct

import pytest

from relaydeck.gateway.backend import BackendError
from relaydeck.gateway.production import JournalBackend
from relaydeck.journal import CommandJournal


@pytest.fixture
def leased(tmp_path):
    clock = [10_000]
    backend = JournalBackend(
        CommandJournal(tmp_path / "input.db", clock=lambda: clock[0]), now_ms=lambda: clock[0]
    )
    attachment, _ = backend.create_attachment(
        "host", "terminal", "agent", "account", "device", takeover=True
    )
    lease = backend.acquire_control(attachment.attachment_id, "account", "device")
    try:
        yield backend, lease
    finally:
        backend.close()


def test_long_input_stream_leaves_nothing_retained(leased):
    backend, lease = leased
    secret = b"hunter2-not-a-real-secret\n"
    for seq in range(1, 5_001):
        assert backend.write_input(lease.lease_id, lease.generation, seq, "text", secret)
    assert backend.write_input(
        lease.lease_id, lease.generation, 5_001, "resize", struct.pack(">HH", 100, 30)
    )
    assert backend.input_logs == {} and backend.resize_log == []


def test_duplicate_and_bad_resize_rejected_without_recording(leased):
    backend, lease = leased
    assert backend.write_input(lease.lease_id, lease.generation, 1, "text", b"a")
    assert not backend.write_input(lease.lease_id, lease.generation, 1, "text", b"a")
    with pytest.raises(BackendError):
        backend.write_input(lease.lease_id, lease.generation, 2, "resize", b"bad")
    with pytest.raises(BackendError):
        backend.write_input(lease.lease_id, lease.generation + 1, 3, "text", b"stale")
    assert backend.input_logs == {} and backend.resize_log == []


def test_recording_is_opt_in(tmp_path):
    backend = JournalBackend(CommandJournal(tmp_path / "rec.db"), record_inputs=True)
    try:
        attachment, _ = backend.create_attachment(
            "host", "terminal", "agent", "account", "device", takeover=True
        )
        lease = backend.acquire_control(attachment.attachment_id, "account", "device")
        backend.write_input(lease.lease_id, lease.generation, 1, "text", b"ls\n")
        assert backend.input_logs[lease.lease_id] == [(lease.generation, 1, "text", b"ls\n")]
    finally:
        backend.close()
