"""Source-backed detach/observe transition; no live Herdr pane or guessed resize."""

import asyncio
import base64
import json
import sys
import time
from typing import Any

import pytest

from relaydeck.connector.attach import AttachError, AttachSession, ObserveSession
from relaydeck.connector.herdr import HerdrAdapter
from relaydeck.connector.source import HerdrSource
from relaydeck.gateway.leases import CONTROL_LEASE_TTL_MS, LeaseStore


def owned() -> tuple[LeaseStore, Any, Any]:
    store = LeaseStore()
    att, _ = store.attach(
        host_id="mac",
        terminal_id="term",
        kind="agent",
        account="acct",
        device_id="phone",
        takeover=True,
        owner_label="Phone",
        now_ms=0,
    )
    lease = store.acquire_control(att.attachment_id, "acct", "phone", 0)
    return store, att, lease


@pytest.mark.parametrize("trigger", ["release", "expiry", "revocation"])
def test_control_end_uses_observer_and_view_cannot_retain_size(trigger: str) -> None:
    store, att, lease = owned()
    view = store.issue_view(att.attachment_id, "acct", "watcher", 0)
    assert store.upstream_mode(att.attachment_id, 0) == "control"
    now = 0
    if trigger == "release":
        store.release(lease.lease_id, "acct", "phone")
    elif trigger == "expiry":
        now = CONTROL_LEASE_TTL_MS + 1
    else:
        store.revoke_device("acct", "phone")
    assert store.upstream_mode(att.attachment_id, now) == "observe"
    assert store.open_lease(view.lease_id, now) is view
    store.release(view.lease_id, "acct", "watcher")
    assert store.upstream_mode(att.attachment_id, now) == "observe"


def test_release_of_old_writer_does_not_detach_new_controller() -> None:
    store, att, old = owned()
    new_att, _ = store.attach(
        host_id="mac",
        terminal_id="term",
        kind="agent",
        account="acct",
        device_id="next",
        takeover=True,
        owner_label="Next",
        now_ms=10,
    )
    new = store.acquire_control(new_att.attachment_id, "acct", "next", 10)
    store.release(old.lease_id, "acct", "phone")
    assert store.upstream_mode(att.attachment_id, 10) == "control"
    assert store.open_lease(new.lease_id, 10) is new
    store.renew(new.lease_id, "acct", "next", 100)
    assert store.upstream_mode(att.attachment_id, CONTROL_LEASE_TTL_MS + 50) == "control"


@pytest.mark.parametrize("kind", ["agent", "shell"])
def test_connector_detaches_before_observing_without_resize_or_write(
    kind: str, monkeypatch: Any
) -> None:
    source = HerdrSource(HerdrAdapter(), subscriptions=[])
    calls: list[str] = []

    class Session(AttachSession):
        @property
        def running(self) -> bool:
            return True

    class Pool:
        def close(self, key: str) -> None:
            if key == f"{kind}:term":
                calls.append("detach")

    source._pool = Pool()  # type: ignore[assignment]

    def attach(terminal: str) -> None:
        calls.append("attach")
        source._attached[f"{kind}:{terminal}"] = (Session([]), terminal, "")

    def observe(terminal: str, _kind: str) -> None:
        calls.append("observe")
        source._attached[f"{kind}:{terminal}"] = (Session([]), terminal, "")

    monkeypatch.setattr(source, "attach_agent", attach)
    monkeypatch.setattr(source, "attach_shell", attach)
    monkeypatch.setattr(source, "observe_terminal", observe)
    assert source.note_epochs({"term": {"epoch": "e1", "kind": kind, "mode": "control"}}) == {}
    assert source.note_epochs({"term": {"epoch": "e1", "kind": kind, "mode": "observe"}}) == {}
    assert calls == ["attach", "detach", "observe"]
    assert source.resize_terminal("term", "e1", 40, 20) == "stale or unavailable terminal"
    assert (
        asyncio.run(source.submit_terminal_input("term", "e1", "text", b"x"))
        == "stale or unavailable terminal"
    )
    source.note_epochs({"term": {"epoch": "e1", "kind": kind, "mode": "observe"}})
    assert calls == ["attach", "detach", "observe"]


def test_observer_decodes_split_frames_preserves_bytes_and_bounds_drain(monkeypatch: Any) -> None:
    payload = b"\x1b[2J" + b"x" * 70000 + "日本語".encode()
    envelope = (
        json.dumps(
            {
                "type": "terminal.frame",
                "seq": 1,
                "encoding": "ansi",
                "width": 120,
                "height": 40,
                "full": True,
                "bytes": base64.b64encode(payload).decode(),
            }
        ).encode()
        + b"\r\n"
    )
    chunks = [envelope[:10], envelope[10:]]
    monkeypatch.setattr(
        AttachSession, "read_available", lambda *_: chunks.pop(0) if chunks else b""
    )
    observer = ObserveSession([])
    assert observer.read_available() == b""
    first = observer.read_available()
    assert len(first) == 65536 and observer.buffered_output
    assert first + observer.read_available() == payload
    assert not observer.buffered_output
    with pytest.raises(AttachError):
        observer.resize(40, 20)
    with pytest.raises(AttachError):
        observer.write(b"x")


@pytest.mark.parametrize(
    "line", [b"not json\n", b'{"type":"terminal.frame","encoding":"ansi","bytes":"!!"}\n']
)
def test_observer_malformed_output_closes_instead_of_forwarding(
    line: bytes, monkeypatch: Any
) -> None:
    monkeypatch.setattr(AttachSession, "read_available", lambda *_: line)
    closed: list[bool] = []
    monkeypatch.setattr(ObserveSession, "close", lambda *_: closed.append(True))
    with pytest.raises(AttachError):
        ObserveSession([]).read_available()
    assert closed == [True]


def test_real_observer_pty_delivers_only_ansi_and_decoded_remainder_wakes_source() -> None:
    payload = b"\x1b[2J\x1b[Hsynthetic observer" + b"x" * 70000
    frame = json.dumps(
        {"type": "terminal.frame", "encoding": "ansi", "bytes": base64.b64encode(payload).decode()}
    )
    session = ObserveSession(
        [
            sys.executable,
            "-c",
            f"import sys,time; sys.stdout.write({frame!r}+'\\n'); "
            "sys.stdout.flush(); time.sleep(10)",
        ]
    )
    session.spawn()
    output = bytearray()
    try:
        deadline = time.monotonic() + 3
        while len(output) < len(payload) and time.monotonic() < deadline:
            output.extend(session.read(65536, timeout=0.1))
        assert bytes(output) == payload
        source = HerdrSource(HerdrAdapter(), subscriptions=[])
        session._decoded.extend(b"tail")
        source._epochs["term"] = "e1"
        source._attached["shell:term"] = (session, "term", "")

        async def ready() -> None:
            await asyncio.wait_for(source.wait_output_ready(), 0.1)

        asyncio.run(ready())
        assert session.read(1) == b"t"
    finally:
        session.close()
