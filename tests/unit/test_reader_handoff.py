"""Fake/durable backend parity and the bounded identity/generation handoff."""

import pytest

from relaydeck.gateway.backend import BackendError, FakeBackend
from relaydeck.gateway.identity import TicketGrant, TicketStore
from relaydeck.gateway.leases import READER_HANDOFF_TTL_MS
from relaydeck.gateway.production import JournalBackend
from relaydeck.journal import CommandJournal


@pytest.fixture(params=["fake", "journal"])
def backend(request, tmp_path):
    clock = [10_000]
    if request.param == "fake":
        backend = FakeBackend(now_ms=lambda: clock[0])
    else:
        backend = JournalBackend(
            CommandJournal(tmp_path / "reader.db", clock=lambda: clock[0]),
            now_ms=lambda: clock[0],
            record_inputs=True,
        )
    attachment, _ = backend.create_attachment(
        "host", "terminal", "agent", "account", "device", takeover=True
    )
    lease = backend.acquire_control(attachment.attachment_id, "account", "device")
    try:
        yield backend, lease, clock
    finally:
        if request.param == "journal":
            backend.close()


def test_handoff_preserves_lease_identity_and_original_expiry_without_control_acquisition(backend):
    backend, lease, clock = backend
    assert backend.begin_reader_handoff(lease.lease_id, "account", "device", lease.generation)
    assert backend.open_lease(lease.lease_id).expires_ms == clock[0] + READER_HANDOFF_TTL_MS
    clock[0] += 1_000
    backend.acquire_control = lambda *_args: pytest.fail("recovery acquired control")
    resumed = backend.finish_reader_handoff(lease.lease_id, "account", "device", lease.generation)
    assert resumed == lease and not resumed.resume_pending
    assert backend.input_logs == {} and backend.resize_log == []


@pytest.mark.parametrize("wrong", ["account", "device", "generation", "view"])
def test_handoff_does_not_keep_foreign_stale_or_view_lease(backend, wrong):
    backend, lease, _clock = backend
    if wrong == "view":
        lease = backend.issue_view(lease.attachment_id, "account", "device")
    account, device, generation = "account", "device", lease.generation
    if wrong == "account":
        account = "other"
    elif wrong == "device":
        device = "other"
    elif wrong == "generation":
        generation += 1
    assert not backend.begin_reader_handoff(lease.lease_id, account, device, generation)
    assert backend.open_lease(lease.lease_id) == lease


def test_handoff_failure_and_renewal_cannot_keep_writer_alive(backend):
    backend, lease, clock = backend
    assert backend.begin_reader_handoff(lease.lease_id, "account", "device", lease.generation)
    until = backend.open_lease(lease.lease_id).expires_ms
    clock[0] += 1_000
    assert backend.begin_reader_handoff(lease.lease_id, "account", "device", lease.generation)
    assert backend.renew_lease(lease.lease_id, "account", "device").expires_ms == until
    for kind, data in [("text", b"never"), ("resize", b"\x00P\x00\x18")]:
        with pytest.raises(BackendError, match="no active writer"):
            backend.write_input(lease.lease_id, lease.generation, 1, kind, data)
    clock[0] = until + 1
    assert backend.open_lease(lease.lease_id) is None
    with pytest.raises(BackendError, match="handoff ended"):
        backend.finish_reader_handoff(lease.lease_id, "account", "device", lease.generation)
    assert backend.epochs_for_host("host", "account")[0]["mode"] == "observe"
    assert backend.input_logs == {} and backend.resize_log == []


@pytest.mark.parametrize("end", ["release", "account", "device", "host", "replacement"])
def test_handoff_does_not_override_release_revocation_or_replacement(backend, end):
    backend, lease, _clock = backend
    assert backend.begin_reader_handoff(lease.lease_id, "account", "device", lease.generation)
    if end == "release":
        backend.release_lease(lease.lease_id, "account", "device")
    elif end == "replacement":
        attachment, _ = backend.create_attachment(
            "host", "terminal", "agent", "account", "other-device", takeover=True
        )
        fresh = backend.acquire_control(attachment.attachment_id, "account", "other-device")
    else:
        args = (
            ("account", "device") if end == "device" else ("host" if end == "host" else "account",)
        )
        getattr(backend, f"revoke_leases_{end}")(*args)
    assert not backend.begin_reader_handoff(lease.lease_id, "account", "device", lease.generation)
    with pytest.raises(BackendError, match="handoff ended"):
        backend.finish_reader_handoff(lease.lease_id, "account", "device", lease.generation)
    if end == "replacement":
        assert backend.open_lease(fresh.lease_id) is not None
    assert backend.input_logs == {} and backend.resize_log == []


def test_pending_ticket_detection_is_identity_generation_expiry_and_single_use_bound():
    store = TicketStore()
    grant = TicketGrant("a", "d", frozenset({"terminal.view"}), frozenset({"h"}), "l", "h", 2, True)
    ticket = store.issue(grant, 1_000)
    assert store.has_resume("l", "a", "d", 2, 1_000)
    assert not store.has_resume("l", "a", "other", 2, 1_000)
    assert not store.has_resume("l", "other", "d", 2, 1_000)
    assert not store.has_resume("l", "a", "d", 3, 1_000)
    assert store.redeem(ticket, "l", 1_000) == grant
    assert not store.has_resume("l", "a", "d", 2, 1_000)
    unused = store.issue(grant, 1_000)
    store.discard_resume("l", "a", "d")
    assert store.redeem(unused, "l", 1_000) is None
    store.issue(grant, 1_000)
    assert not store.has_resume("l", "a", "d", 2, 1_000_000)
