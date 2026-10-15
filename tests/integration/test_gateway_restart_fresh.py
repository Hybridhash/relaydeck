"""Fresh-state restart wipes identity; re-enroll restores.

A gateway restart is a NEW GatewayState (new HostStore, new session and
invitation registries): old host tokens are dead, the tunnel rejects
them (1008 unauthenticated), and the link exits auth-failed instead of
retrying. Recovery is re-enrollment through the shared invitation file,
which DOES survive the restart. No SAME-state illusions: every "after"
assertion runs against a freshly built state. Synthetic loopback only.
"""

from __future__ import annotations

import asyncio
import base64
import time
from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from relaydeck.connector.fixture import FixtureAdapter
from relaydeck.connector.link import ConnectorLink, LinkConfig
from relaydeck.connector.pubkey import generate, sign_proof
from relaydeck.gateway.app import GatewayState, create_app
from relaydeck.gateway.backend import FakeBackend
from relaydeck.gateway.identity import EnrollmentStore
from relaydeck.journal import CommandJournal
from tests.security.helpers import SECRET, auth, make_client, mint_token, snapshot_doc
from tests.support.live_server import LiveGateway

NOW = 1_000_000


def _fresh_state() -> tuple[GatewayState, FakeBackend]:
    """A post-restart server: brand-new process memory, same secrets."""
    backend = FakeBackend(now_ms=lambda: NOW)
    backend.snapshots["home-mac"] = snapshot_doc("sec-cursor-home-mac")
    state = GatewayState(backend=backend, cursor_secret=SECRET, now_ms=lambda: NOW)
    return state, backend


def _redeem_body(
    keys: tuple[bytes, str], invitation_id: str, code: str, host_id: str
) -> dict[str, Any]:
    seed, public = keys
    proof = sign_proof(seed, invitation_id, host_id)
    return {
        "code": code,
        "host_id": host_id,
        "pubkey": public,
        "proof": base64.urlsafe_b64encode(proof).decode().rstrip("="),
    }


def test_restart_wipes_host_tokens_and_tunnel_rejects() -> None:
    client, state, _ = make_client()
    token = mint_token(state, "acct-1")
    keys = generate()
    r = client.post(
        "/v1/enrollments",
        headers=auth(token),
        json={"host_label": "Home Mac", "pubkey": keys[1]},
    )
    assert r.status_code == 201, r.text
    invitation = r.json()
    redeem = client.post(
        f"/v1/enrollments/{invitation['id']}/redeem",
        json=_redeem_body(keys, invitation["id"], invitation["code"], "home-mac"),
    )
    assert redeem.status_code == 200, redeem.text
    old_token: str = redeem.json()["host_token"]

    # --- restart: throw the whole state away, build a fresh one --------
    fresh, _ = _fresh_state()
    assert fresh.hosts.lookup(old_token) is None
    fresh_client = TestClient(create_app(fresh))
    with pytest.raises(WebSocketDisconnect) as exc:
        with fresh_client.websocket_connect(f"/v1/connectors?token={old_token}"):
            pass
    assert exc.value.code == 1008


def test_memory_invitations_die_with_restart() -> None:
    client, state, _ = make_client()
    token = mint_token(state, "acct-1")
    keys = generate()
    r = client.post(
        "/v1/enrollments",
        headers=auth(token),
        json={"host_label": "Home Mac", "pubkey": keys[1]},
    )
    invitation = r.json()

    fresh, _ = _fresh_state()
    fresh_client = TestClient(create_app(fresh))
    redeem = fresh_client.post(
        f"/v1/enrollments/{invitation['id']}/redeem",
        json=_redeem_body(keys, invitation["id"], invitation["code"], "home-mac"),
    )
    assert redeem.json()["code"] == "not_found"


def test_reenroll_after_restart_restores_tunnel(tmp_path: Any) -> None:
    """The documented recovery: a shared-file invitation issued (or
    re-issued) by the operator redeems against the fresh state and the
    new token opens a tunnel."""
    store_path = str(tmp_path / "enrollments.json")
    fresh, _ = _fresh_state()
    fresh.enrollments = EnrollmentStore(path=store_path)
    fresh_client = TestClient(create_app(fresh))

    # Operator side (another process in production): mint + persist.
    cli_side = EnrollmentStore(path=store_path)
    keys = generate()
    from relaydeck.connector.pubkey import fingerprint_of_raw, openssh_to_raw, raw_to_openssh

    raw = openssh_to_raw(keys[1])
    invitation = cli_side.create(
        "acct-1", "Home Mac", raw_to_openssh(raw), fingerprint_of_raw(raw), NOW
    )

    redeem = fresh_client.post(
        f"/v1/enrollments/{invitation.id}/redeem",
        json=_redeem_body(keys, invitation.id, invitation.code, "home-mac"),
    )
    assert redeem.status_code == 200, redeem.text
    new_token: str = redeem.json()["host_token"]
    assert fresh.hosts.lookup(new_token) is not None

    with fresh_client.websocket_connect(f"/v1/connectors?token={new_token}") as ws:
        import json as _json

        hello = _json.loads(ws.receive_text())
        assert hello == {
            "kind": "hello",
            "host_id": "home-mac",
            "snapshot_chunks": True,
            "epochs": [],
        }


def test_link_exits_auth_failed_then_reenrolls(tmp_path: Any) -> None:
    """End to end over a live server: stale token -> auth-failed exit
    (no retry loop), then re-enroll -> once-done session."""
    store_path = str(tmp_path / "enrollments.json")

    async def scenario() -> None:
        fresh, _ = _fresh_state()
        fresh.enrollments = EnrollmentStore(path=store_path)
        with LiveGateway(create_app(fresh)) as live:
            stale = LinkConfig(
                gateway_url=live.base_url,
                host_id="home-mac",
                host_token="rdh_invalid_seed",
            )
            link = ConnectorLink(
                stale,
                FixtureAdapter(),
                journal=CommandJournal(str(tmp_path / "stale.db")),
            )
            started = time.monotonic()
            outcome = await asyncio.wait_for(link.run(asyncio.Event()), timeout=30)
            assert outcome.reason == "auth-failed"
            assert time.monotonic() - started < 15  # exited, never retried
            await link.close()

            # Re-enroll through the shared file against the live server.
            cli_side = EnrollmentStore(path=store_path)
            keys = generate()
            from relaydeck.connector.pubkey import (
                fingerprint_of_raw,
                openssh_to_raw,
                raw_to_openssh,
            )

            raw = openssh_to_raw(keys[1])
            invitation = cli_side.create(
                "acct-1",
                "Home Mac",
                raw_to_openssh(raw),
                fingerprint_of_raw(raw),
                int(time.time() * 1000),
            )
            async with httpx.AsyncClient(base_url=live.base_url) as http:
                redeem = await http.post(
                    f"/v1/enrollments/{invitation.id}/redeem",
                    json=_redeem_body(keys, invitation.id, invitation.code, "home-mac"),
                )
                assert redeem.status_code == 200, redeem.text
                new_token: str = redeem.json()["host_token"]

            healthy = LinkConfig(
                gateway_url=live.base_url,
                host_id="home-mac",
                host_token=new_token,
                once=True,
                once_timeout_s=2.0,
            )
            link2 = ConnectorLink(
                healthy,
                FixtureAdapter(),
                journal=CommandJournal(str(tmp_path / "healthy.db")),
            )
            outcome2 = await asyncio.wait_for(link2.run(asyncio.Event()), timeout=30)
            assert outcome2.reason == "once-done"
            await link2.close()

    asyncio.run(scenario())
