"""Attachment ownership and downstream leases.

Pure store semantics, attach planning/refusal, and the retention pool.
HTTP/WebSocket proof lives in tests/integration/test_terminal_leases.py.
"""

from __future__ import annotations

import stat
import subprocess
import sys
import time
from functools import partial
from pathlib import Path

import pytest

from relaydeck.connector.attach import (
    RETENTION_IDLE_MS,
    AttachError,
    AttachmentPool,
    AttachRefused,
    AttachSession,
    PoolFullError,
    parse_refusal,
    plan_agent_attach,
    plan_shell_attach,
)
from relaydeck.gateway.leases import (
    CONTROL_LEASE_TTL_MS,
    LeaseError,
    LeaseStore,
)

NOW = 1_000_000


def _store() -> LeaseStore:
    return LeaseStore()


def _attach(
    store: LeaseStore,
    kind: str = "shell",
    account: str = "acct-1",
    device: str = "dev-1",
    takeover: bool = False,
    terminal: str = "term-1",
) -> tuple[str, int, str]:
    attachment, _ = store.attach(
        host_id="home-mac",
        terminal_id=terminal,
        kind=kind,
        account=account,
        device_id=device,
        takeover=takeover,
        owner_label=device,
        now_ms=NOW,
    )
    return attachment.attachment_id, attachment.generation, attachment.epoch


# -- first attach authorizes ownership ----------------------------------


def test_first_shell_attach_authorizes_without_takeover() -> None:
    store = _store()
    attachment, displaced = store.attach(
        host_id="home-mac",
        terminal_id="term-1",
        kind="shell",
        account="acct-1",
        device_id="dev-1",
        takeover=False,
        owner_label="dev-1",
        now_ms=NOW,
    )
    assert attachment.generation == 1
    assert attachment.epoch == "e1"
    assert attachment.takeover_authorized is True
    assert displaced == ()
    assert store.get_attachment(attachment.attachment_id) == attachment


def test_shell_attach_refused_when_owned_names_holder() -> None:
    store = _store()
    _attach(store)
    with pytest.raises(LeaseError) as exc:
        store.attach(
            host_id="home-mac",
            terminal_id="term-1",
            kind="shell",
            account="acct-1",
            device_id="dev-2",
            takeover=False,
            owner_label="dev-2",
            now_ms=NOW,
        )
    assert exc.value.code == "lease_conflict"
    assert exc.value.details["owner"]["device_id"] == "dev-1"


def test_cross_account_attach_is_not_found() -> None:
    store = _store()
    _attach(store, account="acct-1", device="dev-1")
    with pytest.raises(LeaseError) as exc:
        store.attach(
            host_id="home-mac",
            terminal_id="term-1",
            kind="shell",
            account="acct-2",
            device_id="dev-9",
            takeover=False,
            owner_label="dev-9",
            now_ms=NOW,
        )
    assert exc.value.code == "not_found"
    assert exc.value.details == {}
    assert "dev-1" not in str(exc.value)


def test_cross_account_takeover_is_not_found_and_untouched() -> None:
    store = _store()
    att_id, _, _ = _attach(store, account="acct-1", device="dev-1")
    control = store.acquire_control(att_id, "acct-1", "dev-1", NOW)
    with pytest.raises(LeaseError) as exc:
        store.attach(
            host_id="home-mac",
            terminal_id="term-1",
            kind="shell",
            account="acct-2",
            device_id="dev-9",
            takeover=True,
            owner_label="dev-9",
            now_ms=NOW,
        )
    assert exc.value.code == "not_found"
    current = store.get_attachment(att_id)
    assert current is not None and current.account == "acct-1"
    assert current.generation == 1
    assert store.check_input(control.lease_id, 1, 1, NOW) is True


def test_agent_attach_requires_explicit_takeover() -> None:
    store = _store()
    with pytest.raises(LeaseError) as exc:
        store.attach(
            host_id="home-mac",
            terminal_id="agent-1",
            kind="agent",
            account="acct-1",
            device_id="dev-1",
            takeover=False,
            owner_label="dev-1",
            now_ms=NOW,
        )
    assert exc.value.code == "forbidden"
    assert exc.value.details == {"reason": "agent_requires_takeover"}
    attachment, _ = store.attach(
        host_id="home-mac",
        terminal_id="agent-1",
        kind="agent",
        account="acct-1",
        device_id="dev-1",
        takeover=True,
        owner_label="dev-1",
        now_ms=NOW,
    )
    assert attachment.kind == "agent"
    assert attachment.takeover_authorized is True


def test_unknown_kind_rejected() -> None:
    store = _store()
    with pytest.raises(LeaseError) as exc:
        store.attach(
            host_id="home-mac",
            terminal_id="term-1",
            kind="exec",
            account="acct-1",
            device_id="dev-1",
            takeover=False,
            owner_label="dev-1",
            now_ms=NOW,
        )
    assert exc.value.code == "forbidden"


# -- explicit takeover ----------------------------------------------------


def test_takeover_bumps_generation_and_displaces_leases() -> None:
    store = _store()
    att_id, _, _ = _attach(store)
    control = store.acquire_control(att_id, "acct-1", "dev-1", NOW)
    view = store.issue_view(att_id, "acct-1", "dev-1", NOW)
    attachment, displaced = store.attach(
        host_id="home-mac",
        terminal_id="term-1",
        kind="shell",
        account="acct-1",
        device_id="dev-2",
        takeover=True,
        owner_label="dev-2",
        now_ms=NOW,
    )
    assert attachment.attachment_id == att_id
    assert attachment.generation == 2
    assert attachment.epoch == "e2"
    assert set(displaced) == {control.lease_id, view.lease_id}
    # Old input callbacks fail as stale (writer replaced), not expired.
    with pytest.raises(LeaseError) as exc:
        store.check_input(control.lease_id, 1, 1, NOW)
    assert exc.value.code == "stale_revision"
    assert store.open_lease(control.lease_id, NOW) is None
    assert store.open_lease(view.lease_id, NOW) is None
    # The new generation is acquirable by the taker.
    fresh = store.acquire_control(att_id, "acct-1", "dev-2", NOW)
    assert fresh.generation == 2
    assert fresh.epoch == "e2"
    assert store.check_input(fresh.lease_id, 2, 1, NOW) is True


def test_same_holder_reattach_bumps_generation() -> None:
    store = _store()
    att_id, _, _ = _attach(store)
    store.attach(
        host_id="home-mac",
        terminal_id="term-1",
        kind="shell",
        account="acct-1",
        device_id="dev-1",
        takeover=True,
        owner_label="dev-1",
        now_ms=NOW,
    )
    current = store.get_attachment(att_id)
    assert current is not None and current.generation == 2


# -- one control writer ----------------------------------------------------


def test_two_devices_cannot_control_concurrently() -> None:
    store = _store()
    att_id, _, _ = _attach(store)
    first = store.acquire_control(att_id, "acct-1", "dev-1", NOW)
    with pytest.raises(LeaseError) as exc:
        store.acquire_control(att_id, "acct-1", "dev-2", NOW)
    assert exc.value.code == "lease_conflict"
    assert exc.value.details == {"holder": {"device_id": "dev-1"}}
    # The first writer is undisturbed.
    assert store.check_input(first.lease_id, 1, 1, NOW) is True


def test_control_conflict_second_device_names_holder() -> None:
    store = _store()
    att_id, _, _ = _attach(store, account="acct-1", device="dev-1")
    store.acquire_control(att_id, "acct-1", "dev-1", NOW)
    # Same-account second devices get a named conflict (no silent
    # steal); cross-account callers cannot name the attachment at all
    # (not_found, covered for attach above and at the HTTP layer).
    with pytest.raises(LeaseError) as exc:
        store.acquire_control(att_id, "acct-1", "dev-2", NOW)
    assert exc.value.code == "lease_conflict"
    assert exc.value.details == {"holder": {"device_id": "dev-1"}}


def test_holder_reacquire_renews_without_conflict() -> None:
    store = _store()
    att_id, _, _ = _attach(store)
    first = store.acquire_control(att_id, "acct-1", "dev-1", NOW)
    again = store.acquire_control(att_id, "acct-1", "dev-1", NOW + 30_000)
    assert again.lease_id == first.lease_id
    assert again.expires_ms == NOW + 30_000 + CONTROL_LEASE_TTL_MS


def test_expired_control_is_replaced_not_resurrected() -> None:
    store = _store()
    att_id, _, _ = _attach(store)
    first = store.acquire_control(att_id, "acct-1", "dev-1", NOW)
    later = NOW + CONTROL_LEASE_TTL_MS + 1
    second = store.acquire_control(att_id, "acct-1", "dev-2", later)
    assert second.lease_id != first.lease_id
    with pytest.raises(LeaseError) as exc:
        store.check_input(first.lease_id, 1, 1, later)
    assert exc.value.code == "lease_expired"


# -- views are read-only ----------------------------------------------------


def test_view_lease_has_no_writer() -> None:
    store = _store()
    att_id, _, _ = _attach(store)
    view = store.issue_view(att_id, "acct-1", "dev-2", NOW)
    assert view.mode == "view"
    assert store.open_lease(view.lease_id, NOW) is not None
    for seq in (1, 2):
        with pytest.raises(LeaseError) as exc:
            store.check_input(view.lease_id, 1, seq, NOW)
        assert exc.value.code == "lease_conflict"


def test_view_requires_existing_attachment() -> None:
    store = _store()
    with pytest.raises(LeaseError) as exc:
        store.issue_view("att-999", "acct-1", "dev-1", NOW)
    assert exc.value.code == "not_found"


def test_many_views_one_control() -> None:
    store = _store()
    att_id, _, _ = _attach(store)
    store.acquire_control(att_id, "acct-1", "dev-1", NOW)
    views = {store.issue_view(att_id, "acct-1", f"dev-{i}", NOW).lease_id for i in (2, 3, 4)}
    assert len(views) == 3


# -- detach vs close ----------------------------------------------------------


def test_detach_keeps_attachment_live() -> None:
    store = _store()
    att_id, _, _ = _attach(store)
    control = store.acquire_control(att_id, "acct-1", "dev-1", NOW)
    attachment = store.release(control.lease_id, "acct-1", "dev-1")
    assert attachment.closed is False
    assert store.open_lease(control.lease_id, NOW) is None
    # Another device can take control of the surviving stream.
    second = store.acquire_control(att_id, "acct-1", "dev-2", NOW)
    assert second.generation == 1


def test_release_unknown_or_foreign_is_not_found() -> None:
    store = _store()
    att_id, _, _ = _attach(store)
    control = store.acquire_control(att_id, "acct-1", "dev-1", NOW)
    with pytest.raises(LeaseError) as exc:
        store.release("lease-999", "acct-1", "dev-1")
    assert exc.value.code == "not_found"
    with pytest.raises(LeaseError) as exc:
        store.release(control.lease_id, "acct-2", "dev-9")
    assert exc.value.code == "not_found"
    assert store.open_lease(control.lease_id, NOW) is not None


def test_release_by_other_device_is_not_found_no_steal() -> None:
    store = _store()
    att_id, _, _ = _attach(store)
    control = store.acquire_control(att_id, "acct-1", "dev-1", NOW)
    # A same-account second device cannot release another device's lease.
    with pytest.raises(LeaseError) as exc:
        store.release(control.lease_id, "acct-1", "dev-2")
    assert exc.value.code == "not_found"
    # ... and therefore cannot acquire control at the same generation.
    with pytest.raises(LeaseError) as exc:
        store.acquire_control(att_id, "acct-1", "dev-2", NOW)
    assert exc.value.code == "lease_conflict"
    assert exc.value.details == {"holder": {"device_id": "dev-1"}}
    # The first writer is undisturbed.
    assert store.check_input(control.lease_id, 1, 1, NOW) is True


def test_close_ends_attachment_and_expires_leases() -> None:
    store = _store()
    att_id, _, _ = _attach(store)
    control = store.acquire_control(att_id, "acct-1", "dev-1", NOW)
    view = store.issue_view(att_id, "acct-1", "dev-2", NOW)
    dead = store.close_attachment(att_id, "acct-1")
    assert set(dead) == {control.lease_id, view.lease_id}
    with pytest.raises(LeaseError) as exc:
        store.check_input(control.lease_id, 1, 1, NOW)
    assert exc.value.code == "lease_expired"
    with pytest.raises(LeaseError) as exc:
        store.acquire_control(att_id, "acct-1", "dev-1", NOW)
    assert exc.value.code == "lease_expired"
    # Re-attach after close starts a new attachment identity.
    attachment, _ = store.attach(
        host_id="home-mac",
        terminal_id="term-1",
        kind="shell",
        account="acct-1",
        device_id="dev-1",
        takeover=False,
        owner_label="dev-1",
        now_ms=NOW,
    )
    assert attachment.attachment_id != att_id
    assert attachment.generation > control.generation


# -- revocation -----------------------------------------------------------------


def test_revoke_account_device_host_invalidate_leases() -> None:
    store = _store()
    att_id, _, _ = _attach(store)
    mine = store.acquire_control(att_id, "acct-1", "dev-1", NOW)
    other = store.issue_view(att_id, "acct-1", "dev-2", NOW)
    assert store.revoke_device("acct-1", "dev-2") == (other.lease_id,)
    assert store.open_lease(other.lease_id, NOW) is None
    assert store.open_lease(mine.lease_id, NOW) is not None
    assert set(store.revoke_host("home-mac")) == {mine.lease_id}
    assert store.open_lease(mine.lease_id, NOW) is None
    with pytest.raises(LeaseError) as exc:
        store.check_input(mine.lease_id, 1, 1, NOW)
    assert exc.value.code == "lease_expired"


def test_revoke_account_clears_everything() -> None:
    store = _store()
    att_id, _, _ = _attach(store)
    first = store.acquire_control(att_id, "acct-1", "dev-1", NOW)
    second = store.issue_view(att_id, "acct-1", "dev-1", NOW)
    assert set(store.revoke_account("acct-1")) == {first.lease_id, second.lease_id}
    assert store.open_lease(first.lease_id, NOW) is None


# -- generations, expiry, sequences ----------------------------------------------


def test_generation_mismatch_is_stale() -> None:
    store = _store()
    att_id, _, _ = _attach(store)
    control = store.acquire_control(att_id, "acct-1", "dev-1", NOW)
    assert store.check_input(control.lease_id, 1, 1, NOW) is True
    with pytest.raises(LeaseError) as exc:
        store.check_input(control.lease_id, 0, 2, NOW)
    assert exc.value.code == "stale_revision"
    assert exc.value.retryable is True
    with pytest.raises(LeaseError) as exc:
        store.check_input(control.lease_id, 99, 2, NOW)
    assert exc.value.code == "stale_revision"


def test_control_expiry_and_renewal() -> None:
    store = _store()
    att_id, _, _ = _attach(store)
    control = store.acquire_control(att_id, "acct-1", "dev-1", NOW)
    assert control.expires_ms == NOW + CONTROL_LEASE_TTL_MS
    renewed = store.renew(control.lease_id, "acct-1", "dev-1", NOW + 59_000)
    assert renewed.expires_ms == NOW + 59_000 + CONTROL_LEASE_TTL_MS
    with pytest.raises(LeaseError) as exc:
        store.renew(control.lease_id, "acct-1", "dev-2", NOW + 59_000)
    assert exc.value.code == "not_found"
    expired_at = NOW + 59_000 + CONTROL_LEASE_TTL_MS + 1
    with pytest.raises(LeaseError) as exc:
        store.check_input(control.lease_id, 1, 1, expired_at)
    assert exc.value.code == "lease_expired"
    with pytest.raises(LeaseError) as exc:
        store.renew(control.lease_id, "acct-1", "dev-1", expired_at)
    assert exc.value.code == "lease_expired"


def test_input_seq_dedupes_within_generation() -> None:
    store = _store()
    att_id, _, _ = _attach(store)
    control = store.acquire_control(att_id, "acct-1", "dev-1", NOW)
    assert store.check_input(control.lease_id, 1, 1, NOW) is True
    assert store.check_input(control.lease_id, 1, 1, NOW) is False
    assert store.check_input(control.lease_id, 1, 2, NOW) is True
    # Takeover resets the high-water mark with the new generation.
    store.attach(
        host_id="home-mac",
        terminal_id="term-1",
        kind="shell",
        account="acct-1",
        device_id="dev-2",
        takeover=True,
        owner_label="dev-2",
        now_ms=NOW,
    )
    fresh = store.acquire_control(att_id, "acct-1", "dev-2", NOW)
    assert store.check_input(fresh.lease_id, 2, 1, NOW) is True


def test_legacy_issue_pins_generation_and_replaces() -> None:
    store = _store()
    first = store.legacy_issue("home-mac", "term-1", "tester", 5, "e1", NOW)
    assert (first.generation, first.epoch, first.mode) == (5, "e1", "control")
    assert store.check_input(first.lease_id, 5, 1, NOW) is True
    second = store.legacy_issue("home-mac", "term-1", "tester", 9, "e1", NOW)
    with pytest.raises(LeaseError) as exc:
        store.check_input(first.lease_id, 5, 2, NOW)
    assert exc.value.code == "stale_revision"
    assert store.check_input(second.lease_id, 9, 1, NOW) is True


# -- attach planning ---------------------------------------------------------------


def test_plan_shell_attach_omits_takeover() -> None:
    assert plan_shell_attach("herdr", "term-1") == ["herdr", "terminal", "attach", "term-1"]
    with pytest.raises(AttachError):
        plan_shell_attach("herdr", "term-1", cols=0, rows=24)


def test_plan_agent_attach_requires_explicit_takeover() -> None:
    with pytest.raises(AttachError):
        plan_agent_attach("herdr", "w1:p1", takeover=False)
    assert plan_agent_attach("herdr", "w1:p1", takeover=True) == [
        "herdr",
        "agent",
        "attach",
        "w1:p1",
        "--takeover",
    ]


def test_parse_refusal_is_format_agnostic() -> None:
    assert parse_refusal(b"error: owned by office-mac\nmore\n") == "error: owned by office-mac"
    assert parse_refusal(b"\n\n  \nfirst\nsecond\n") == "first"
    assert parse_refusal(b"\x00\x01\x02") == ""
    assert parse_refusal(b"") == ""
    assert len(parse_refusal(b"x" * 500)) == 128


# -- retention pool -------------------------------------------------------------------


class _FakeSession:
    def __init__(self) -> None:
        self.closed = 0

    def close(self) -> int | None:
        self.closed += 1
        return 0


def test_pool_retains_five_and_lru_evicts_sixth() -> None:
    pool = AttachmentPool()
    made: dict[str, _FakeSession] = {}

    def factory(key: str) -> _FakeSession:
        session = _FakeSession()
        made[key] = session
        return session

    for i in range(5):
        pool.acquire(f"t{i}", partial(factory, f"t{i}"), NOW + i)
    assert len(pool) == 5
    pool.release("t0", NOW + 100)
    pool.acquire("t5", partial(factory, "t5"), NOW + 200)
    assert len(pool) == 5
    assert made["t0"].closed == 1
    assert "t0" not in pool.keys()
    assert "t5" in pool.keys()


def test_pool_all_visible_fails_admission() -> None:
    pool = AttachmentPool()
    for i in range(5):
        pool.acquire(f"t{i}", _FakeSession, NOW)
    pool.set_visible([f"t{i}" for i in range(5)])
    with pytest.raises(PoolFullError) as exc:
        pool.acquire("t5", _FakeSession, NOW)
    assert exc.value.visible == 5
    assert len(pool) == 5


def test_pool_idle_expiry_protects_visible() -> None:
    pool = AttachmentPool()
    a = pool.acquire("a", _FakeSession, NOW)
    b = pool.acquire("b", _FakeSession, NOW)
    assert isinstance(a, _FakeSession) and isinstance(b, _FakeSession)
    pool.release("a", NOW)
    pool.release("b", NOW)
    pool.set_visible(["b"])
    assert pool.evict_idle(NOW + RETENTION_IDLE_MS - 1) == ()
    assert pool.evict_idle(NOW + RETENTION_IDLE_MS) == ("a",)
    assert a.closed == 1
    assert b.closed == 0
    assert pool.keys() == ("b",)


def test_pool_release_is_detach_not_close() -> None:
    pool = AttachmentPool()
    session = pool.acquire("t0", _FakeSession, NOW)
    assert isinstance(session, _FakeSession)
    pool.release("t0", NOW)
    assert session.closed == 0  # still live upstream, retained
    assert len(pool) == 1
    assert pool.acquire("t0", _FakeSession, NOW + 10) is session
    pool.close("t0")
    assert session.closed == 1
    pool.close("t0")  # idempotent
    assert session.closed == 1


# -- stub-binary attach sessions (no live daemon writes) ---------------------------------


_ECHO_STUB = """#!{python}
import sys
sys.stdout.write("stub-ready\\r\\n")
sys.stdout.flush()
while True:
    data = sys.stdin.readline()
    if not data:
        break
    sys.stdout.write("echo:" + data)
    sys.stdout.flush()
"""

_REFUSE_STUB = """#!{python}
import sys
sys.stdout.write("error: terminal owned by office-dev\\n")
sys.stdout.flush()
sys.exit(3)
"""


def _stub(tmp_path: Path, name: str, body: str) -> str:
    path = tmp_path / name
    path.write_text(body.format(python=sys.executable))
    path.chmod(path.stat().st_mode | stat.S_IEXEC)
    return str(path)


def test_expect_ready_refusal_carries_hint(tmp_path: Path) -> None:
    session = AttachSession(argv=[_stub(tmp_path, "refuse-herdr", _REFUSE_STUB)])
    session.spawn()
    try:
        with pytest.raises(AttachRefused) as exc:
            session.expect_ready(b"stub-ready", timeout=5.0)
        assert exc.value.code == "lease_conflict"
        assert exc.value.owner_hint == "error: terminal owned by office-dev"
        assert session.exit_status == 3
    finally:
        session.close()


def test_expect_ready_success_then_detach(tmp_path: Path) -> None:
    session = AttachSession(argv=[_stub(tmp_path, "echo-herdr", _ECHO_STUB)])
    session.spawn()
    try:
        banner = session.expect_ready(b"stub-ready", timeout=5.0)
        assert b"stub-ready" in banner
        session.write(b"hi\n")
        deadline = time.monotonic() + 5.0
        seen = b""
        while b"echo:hi" not in seen and time.monotonic() < deadline:
            seen += session.read(timeout=0.2)
        assert b"echo:hi" in seen
    finally:
        session.detach()
    assert session.running is False


def test_detach_kills_client_not_remote(tmp_path: Path) -> None:
    pane = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
    try:
        session = AttachSession(argv=[_stub(tmp_path, "echo-herdr", _ECHO_STUB)])
        session.spawn()
        session.expect_ready(b"stub-ready", timeout=5.0)
        session.detach()
        assert session.running is False
        assert pane.poll() is None  # the remote process survives detach
    finally:
        pane.terminate()
        pane.wait(timeout=5.0)
