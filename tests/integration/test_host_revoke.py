"""Operable single-host revoke via the operator spool.

`revoke-host --spool` queues a request file; the serving process drains
the spool and revokes exactly that host (tunnels + leases severed,
every other host untouched). The serving-process drain loop lives in
app.py (coordinator-owned); this file proves
the CLI writer, the spool format, and the drain+revoke round-trip
against a real GatewayState. Synthetic loopback only.
"""

from __future__ import annotations

import asyncio
import json
import os
import stat
from typing import Any

from relaydeck.gateway import cli as gateway_cli
from relaydeck.gateway.app import GatewayState
from relaydeck.gateway.hosts import drain_revoke_requests, write_revoke_request
from relaydeck.gateway.revocation import revoke_host_and_close
from tests.security.helpers import make_state
from tests.support.gateway.fake_socket import _FakeSocket


def test_revoke_cli_queues_request(tmp_path: Any, capsys: Any) -> None:
    spool = str(tmp_path / "spool")
    assert gateway_cli.main(["revoke-host", "--host", "home-mac", "--spool", spool]) == 0
    names = os.listdir(spool)
    assert len(names) == 1 and names[0].endswith(".json")
    path = os.path.join(spool, names[0])
    assert stat.S_IMODE(os.stat(path).st_mode) == 0o600
    assert stat.S_IMODE(os.stat(spool).st_mode) == 0o700
    with open(path, encoding="utf-8") as handle:
        payload = json.load(handle)
    assert payload["host_id"] == "home-mac"
    assert payload["version"] == 1
    assert "revoke queued for home-mac" in capsys.readouterr().out


def test_revoke_cli_without_spool_exits_2() -> None:
    assert gateway_cli.main(["revoke-host", "--host", "home-mac"]) == 2


def test_revoke_cli_empty_host_exits_2(tmp_path: Any) -> None:
    assert gateway_cli.main(["revoke-host", "--host", "  ", "--spool", str(tmp_path)]) == 2


def test_drain_revokes_single_host_only(tmp_path: Any) -> None:
    state, backend = make_state(hosts=("home-mac", "office-mac"))
    token_home = state.hosts.enroll("home-mac", "Home", "acct-1")
    token_office = state.hosts.enroll("office-mac", "Office", "acct-1")
    assert state.hosts.lookup(token_home) is not None
    assert state.hosts.lookup(token_office) is not None

    spool = str(tmp_path / "spool")
    write_revoke_request(spool, "home-mac", now_ms=1_000_000)
    assert drain_revoke_requests(spool) == ("home-mac",)
    assert drain_revoke_requests(spool) == ()  # consumed exactly once

    revoked, tunnels, _ = asyncio.run(revoke_host_and_close(state, "home-mac"))
    assert revoked is True
    assert tunnels == 0
    assert state.hosts.lookup(token_home) is None  # dead immediately
    assert state.hosts.lookup(token_office) is not None  # untouched


def test_drain_severs_tunnels_and_leases(tmp_path: Any) -> None:
    state, backend = make_state(hosts=("home-mac", "office-mac"))
    state.hosts.enroll("home-mac", "Home", "acct-1")
    state.hosts.enroll("office-mac", "Office", "acct-1")
    tunnel = _FakeSocket()
    state.sockets.register_host("home-mac", tunnel)
    other_tunnel = _FakeSocket()
    state.sockets.register_host("office-mac", other_tunnel)
    attachment, _ = backend.create_attachment(
        "home-mac", "term-1", "shell", "acct-1", "dev-1", takeover=False
    )
    lease = backend.acquire_control(attachment.attachment_id, "acct-1", "dev-1")
    viewer = _FakeSocket()
    state.sockets.register("acct-1", lease.lease_id, viewer, "dev-1")

    spool = str(tmp_path / "spool")
    write_revoke_request(spool, "home-mac", now_ms=1_000_000)
    assert drain_revoke_requests(spool) == ("home-mac",)
    revoked, tunnels, leases = asyncio.run(revoke_host_and_close(state, "home-mac"))

    assert (revoked, tunnels, leases) == (True, 1, 1)
    assert tunnel.closes == [(1008, "unauthenticated")]
    assert viewer.closes == [(1008, "lease_expired")]
    assert other_tunnel.closes == []
    assert backend.open_lease(lease.lease_id) is None
    assert state.hosts.record_for_host("office-mac") is not None


def test_revoke_unknown_host_is_explicit_miss() -> None:
    state, _ = make_state(hosts=("home-mac",))
    assert asyncio.run(revoke_host_and_close(state, "ghost-mac")) == (False, 0, 0)


def test_malformed_requests_quarantined(tmp_path: Any) -> None:
    spool = str(tmp_path / "spool")
    os.makedirs(spool)
    with open(os.path.join(spool, "revoke-aaa.json"), "w", encoding="utf-8") as handle:
        handle.write("{not json")
    with open(os.path.join(spool, "revoke-bbb.json"), "w", encoding="utf-8") as handle:
        json.dump({"version": 999, "host_id": "home-mac"}, handle)
    with open(os.path.join(spool, "partial.tmp"), "w", encoding="utf-8") as handle:
        handle.write("incomplete write")
    assert drain_revoke_requests(spool) == ()
    names = sorted(os.listdir(spool))
    assert names == ["partial.tmp", "revoke-aaa.json.bad", "revoke-bbb.json.bad"]
    assert drain_revoke_requests(spool) == ()  # never retried forever


def test_missing_spool_drains_empty(tmp_path: Any) -> None:
    assert drain_revoke_requests(str(tmp_path / "no-such-dir")) == ()


def test_duplicate_requests_dedupe(tmp_path: Any) -> None:
    spool = str(tmp_path / "spool")
    write_revoke_request(spool, "home-mac", now_ms=1)
    write_revoke_request(spool, "home-mac", now_ms=2)
    write_revoke_request(spool, "office-mac", now_ms=3)
    drained = drain_revoke_requests(spool)
    assert sorted(drained) == ["home-mac", "office-mac"]
    assert len(drained) == 2


def test_state_type_pins_hook_contract() -> None:
    """The proposed app.py hook only needs GatewayState + these two
    callables; pin their import surface so the proposal cannot drift."""
    import relaydeck.gateway.hosts as hosts

    assert callable(hosts.drain_revoke_requests)
    assert callable(hosts.write_revoke_request)
    assert isinstance(GatewayState.__dataclass_fields__["hosts"], object)
