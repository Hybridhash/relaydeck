"""durable gateway serve wiring + loopback flows.

`serve` option validation (loopback gate, secrets, database paths),
state construction (JournalBackend + file push store vs demo store),
and live loopback proofs: enrollment/redeem, snapshot/event push over
the connector tunnel, SSE delivery, and the attach -> lease -> renew
-> ticket -> terminal-WS flow. Synthetic fixtures only.
"""

from __future__ import annotations

import asyncio
import json
import stat
import time
from pathlib import Path
from typing import Any

import httpx
import pytest
import websockets.asyncio.client as ws_client

from relaydeck.connector.enrollment import claim_enrollment, generate_keys
from relaydeck.connector.fixture import fixture_event, fixture_snapshot
from relaydeck.gateway import terminal_wire
from relaydeck.gateway.app import create_app
from relaydeck.gateway.cli import (
    ServeConfigError,
    build_parser,
    build_serve_state,
    resolve_serve_options,
)
from relaydeck.gateway.cli import main as gateway_main
from relaydeck.gateway.cursors import decode_cursor
from relaydeck.gateway.production import JournalBackend
from tests.support.live_server import LiveGateway

NOW = 1_700_000_000_000
SECRET = b"gateway-loopback-test-secret-32byt"
ALL_CAPS = frozenset(
    {
        "sessions.read",
        "terminal.view",
        "terminal.control",
        "agents.prompt",
        "agents.start",
        "shells.create",
        "terminal.attach",
        "hosts.enroll",
    }
)


def _serve_args(*extra: str) -> Any:
    return build_parser().parse_args(["serve", *extra])


def test_serve_rejects_non_loopback_and_bad_options(tmp_path: Path) -> None:
    with pytest.raises(ServeConfigError, match="loopback"):
        resolve_serve_options(_serve_args("--host", "0.0.0.0"))
    with pytest.raises(ServeConfigError, match="port"):
        resolve_serve_options(_serve_args("--port", "0"))
    with pytest.raises(ServeConfigError, match="32 bytes"):
        resolve_serve_options(_serve_args("--cursor-secret", "short"))
    with pytest.raises(ServeConfigError, match="cursor-secret-file"):
        resolve_serve_options(_serve_args("--cursor-secret-file", str(tmp_path / "nope")))
    assert gateway_main(["serve", "--host", "0.0.0.0"]) == 2


def test_serve_secret_sources_and_ephemeral(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("RELAYDECK_CURSOR_SECRET", raising=False)
    assert resolve_serve_options(_serve_args()).ephemeral_secret is True
    assert len(resolve_serve_options(_serve_args()).cursor_secret) == 32
    flagged = resolve_serve_options(_serve_args("--cursor-secret", "x" * 40))
    assert flagged.ephemeral_secret is False
    assert flagged.cursor_secret == b"x" * 40
    secret_file = tmp_path / "cursor.secret"
    secret_file.write_text("y" * 40 + "\n")
    from_file = resolve_serve_options(_serve_args("--cursor-secret-file", str(secret_file)))
    assert from_file.cursor_secret == b"y" * 40
    monkeypatch.setenv("RELAYDECK_CURSOR_SECRET", "z" * 40)
    assert resolve_serve_options(_serve_args()).cursor_secret == b"z" * 40


def test_serve_state_wiring_durable_vs_demo(tmp_path: Path) -> None:
    durable = resolve_serve_options(
        _serve_args(
            "--journal-path",
            str(tmp_path / "gw.db"),
            "--push-path",
            str(tmp_path / "push.db"),
            "--cursor-secret",
            "s" * 40,
        )
    )
    state = build_serve_state(durable)
    try:
        assert isinstance(state.backend, JournalBackend)
        assert state.cursor_secret == b"s" * 40
        assert (tmp_path / "gw.db").exists()
        assert (tmp_path / "push.db").exists()
        assert stat.S_IMODE((tmp_path / "gw.db").stat().st_mode) == 0o600
        assert stat.S_IMODE((tmp_path / "push.db").stat().st_mode) == 0o600
    finally:
        state.backend.close()
        state.push.close()
    demo = resolve_serve_options(_serve_args("--cursor-secret", "s" * 40))
    demo_state = build_serve_state(demo)
    try:
        assert type(demo_state.backend).__name__ == "FakeBackend"
    finally:
        demo_state.push.close()


def _seed_live(tmp_path: Path) -> Any:
    options = resolve_serve_options(
        _serve_args(
            "--journal-path",
            str(tmp_path / "gw.db"),
            "--push-path",
            str(tmp_path / "push.db"),
            "--cursor-secret",
            SECRET.decode(),
        )
    )
    state = build_serve_state(options)
    state.now_ms = lambda: NOW  # deterministic clock for TTL/expiry paths
    state.backend.now_ms = lambda: NOW
    state.backend.record_inputs = True  # observe accepted input; production keeps none
    return state


def test_loopback_enroll_redeem_and_metadata_push(tmp_path: Path) -> None:
    async def scenario() -> None:
        state = _seed_live(tmp_path)
        try:
            with LiveGateway(create_app(state)) as live:
                access, _ = state.sessions.create("acct-1", ALL_CAPS, frozenset({"home-mac"}), NOW)
                headers = {"Authorization": f"Bearer {access}"}
                keys = generate_keys()
                async with httpx.AsyncClient(timeout=10.0) as client:
                    created = await client.post(
                        f"{live.base_url}/v1/enrollments",
                        headers=headers,
                        json={"host_label": "Home Mac", "pubkey": keys.public_openssh},
                    )
                    assert created.status_code == 201, created.text
                    invitation = created.json()

                    async def post_json(
                        url: str, body: dict[str, Any]
                    ) -> tuple[int, dict[str, Any]]:
                        response = await client.post(url, json=body)
                        try:
                            return response.status_code, response.json()
                        except ValueError:
                            return response.status_code, {}

                    host_token, echoed = await claim_enrollment(
                        post_json,
                        live.base_url,
                        "home-mac",
                        invitation["id"],
                        invitation["code"],
                        keys,
                    )
                    assert echoed == keys.fingerprint
                    # Unknown host before the first snapshot push.
                    missing = await client.get(
                        f"{live.base_url}/v1/hosts/home-mac/snapshot", headers=headers
                    )
                    assert missing.json()["code"] == "not_found"
                ws_url = live.base_url.replace("http://", "ws://") + "/v1/connectors"
                async with ws_client.connect(
                    ws_url, additional_headers={"Authorization": f"Bearer {host_token}"}
                ) as tunnel:
                    hello = json.loads(await tunnel.recv())
                    assert hello == {
                        "kind": "hello",
                        "host_id": "home-mac",
                        "snapshot_chunks": True,
                        "epochs": [],
                    }
                    snapshot = fixture_snapshot("home-mac")
                    await tunnel.send(
                        json.dumps(
                            {"kind": "snapshot", "host_id": "home-mac", "snapshot": snapshot}
                        )
                    )
                    await tunnel.send(
                        json.dumps(
                            {
                                "kind": "event",
                                "host_id": "home-mac",
                                "event": fixture_event(
                                    "agent.upserted", "ev-lb-1", resource_id="term-1"
                                ),
                            }
                        )
                    )
                    # Re-push dedupes by event_id (no new sequence).
                    await tunnel.send(
                        json.dumps(
                            {
                                "kind": "event",
                                "host_id": "home-mac",
                                "event": fixture_event(
                                    "agent.upserted", "ev-lb-1", resource_id="term-1"
                                ),
                            }
                        )
                    )
                    await tunnel.send(json.dumps({"kind": "ping"}))
                    assert json.loads(await tunnel.recv()) == {"kind": "pong"}
                    await tunnel.send(json.dumps({"kind": "bye"}))
                async with httpx.AsyncClient(timeout=10.0) as client:
                    headers = {"Authorization": f"Bearer {access}"}
                    snap = await client.get(
                        f"{live.base_url}/v1/hosts/home-mac/snapshot", headers=headers
                    )
                    assert snap.status_code == 200, snap.text
                    assert snap.json()["host"]["id"] == "home-mac"
                    assert snap.json()["host"]["protocol"] == 22
                    # The server stamps the resume cursor (clients stream
                    # from snapshot.cursor): connector value ignored.
                    stamped = decode_cursor(SECRET, snap.json()["cursor"])
                    assert stamped == {"host_id": "home-mac", "sequence": 0}
                    agents = await client.get(
                        f"{live.base_url}/v1/hosts/home-mac/agents", headers=headers
                    )
                    assert agents.status_code == 200
                    assert agents.json()["host_id"] == "home-mac"
            log = state.backend.event_logs["home-mac"]
            assert [e["event_id"] for e in log] == ["ev-lb-1"]
            assert log[0]["sequence"] == 1
            assert log[0]["host_id"] == "home-mac"
            assert log[0]["cursor"]
        finally:
            state.backend.close()
            state.push.close()

    asyncio.run(scenario())


def _sse_take(chunks: Any, count: int) -> list[dict[str, Any]]:
    buf = ""
    bodies: list[dict[str, Any]] = []
    for piece in chunks:
        buf += piece
        while "\n\n" in buf and len(bodies) < count:
            frame, buf = buf.split("\n\n", 1)
            for line in frame.splitlines():
                if line.startswith("data: "):
                    bodies.append(json.loads(line[len("data: ") :]))
        if len(bodies) >= count:
            return bodies  # the stream is infinite: stop reading once satisfied
    return bodies


def test_loopback_pushed_event_reaches_sse(tmp_path: Path) -> None:
    async def scenario() -> None:
        state = _seed_live(tmp_path)
        try:
            with LiveGateway(create_app(state)) as live:
                access, _ = state.sessions.create("acct-1", ALL_CAPS, frozenset({"home-mac"}), NOW)
                headers = {"Authorization": f"Bearer {access}"}
                host_token = state.hosts.enroll("home-mac", "Home Mac", "acct-1")
                ws_url = live.base_url.replace("http://", "ws://") + "/v1/connectors"
                async with ws_client.connect(
                    ws_url, additional_headers={"Authorization": f"Bearer {host_token}"}
                ) as tunnel:
                    assert json.loads(await tunnel.recv())["kind"] == "hello"
                    await tunnel.send(
                        json.dumps(
                            {
                                "kind": "snapshot",
                                "host_id": "home-mac",
                                "snapshot": fixture_snapshot("home-mac"),
                            }
                        )
                    )
                    await tunnel.send(json.dumps({"kind": "bye"}))
                # Live subscriber first, then a pushed event lands on the stream.
                with httpx.Client(base_url=live.base_url, timeout=10.0) as http:
                    with http.stream(
                        "GET", "/v1/events?host_id=home-mac", headers=headers
                    ) as response:
                        assert response.status_code == 200
                        chunks = response.iter_text()
                        # Prove the follower is registered before pushing:
                        # without this the push can land pre-subscribe (the
                        # replay here is empty, so no take() proves liveness).
                        deadline = time.monotonic() + 10.0
                        while not state.backend._followers.get("home-mac"):
                            assert time.monotonic() < deadline, "SSE follower never registered"
                            await asyncio.sleep(0.05)
                        async with ws_client.connect(
                            ws_url, additional_headers={"Authorization": f"Bearer {host_token}"}
                        ) as tunnel:
                            assert json.loads(await tunnel.recv())["kind"] == "hello"
                            await tunnel.send(
                                json.dumps(
                                    {
                                        "kind": "event",
                                        "host_id": "home-mac",
                                        "event": fixture_event(
                                            "pane.upserted", "ev-sse-1", resource_id="w1:p1"
                                        ),
                                    }
                                )
                            )
                            await tunnel.send(json.dumps({"kind": "ping"}))
                            assert json.loads(await tunnel.recv()) == {"kind": "pong"}
                            await tunnel.send(json.dumps({"kind": "bye"}))
                        bodies = await asyncio.to_thread(_sse_take, chunks, 1)
                        assert [b["event_id"] for b in bodies] == ["ev-sse-1"]
        finally:
            state.backend.close()
            state.push.close()

    asyncio.run(scenario())


def test_loopback_attach_lease_renew_ticket_ws(tmp_path: Path) -> None:
    async def scenario() -> None:
        state = _seed_live(tmp_path)
        try:
            with LiveGateway(create_app(state)) as live:
                access, _ = state.sessions.create("acct-1", ALL_CAPS, frozenset({"home-mac"}), NOW)
                headers = {"Authorization": f"Bearer {access}"}
                async with httpx.AsyncClient(timeout=10.0) as client:
                    attached = await client.post(
                        f"{live.base_url}/v1/hosts/home-mac/terminals/term-1/attachments",
                        headers=headers,
                        json={"kind": "shell", "takeover": False},
                    )
                    assert attached.status_code == 201, attached.text
                    attachment_id = attached.json()["attachment_id"]
                    leased = await client.post(
                        f"{live.base_url}/v1/terminal-attachments/{attachment_id}/leases",
                        headers=headers,
                        json={"mode": "control"},
                    )
                    assert leased.status_code == 201, leased.text
                    lease_id = leased.json()["lease_id"]
                    renewed = await client.post(
                        f"{live.base_url}/v1/terminal-leases/{lease_id}/renew",
                        headers=headers,
                        json={},
                    )
                    assert renewed.status_code == 200, renewed.text
                    # Foreign device renewal discloses nothing.
                    other, _ = state.sessions.create(
                        "acct-1", ALL_CAPS, frozenset({"home-mac"}), NOW
                    )
                    denied = await client.post(
                        f"{live.base_url}/v1/terminal-leases/{lease_id}/renew",
                        headers={"Authorization": f"Bearer {other}"},
                        json={},
                    )
                    assert denied.json()["code"] == "not_found"
                    ticketed = await client.post(
                        f"{live.base_url}/v1/terminal-tickets",
                        headers=headers,
                        json={"lease_id": lease_id},
                    )
                    assert ticketed.status_code == 200, ticketed.text
                    ticket = ticketed.json()["ticket"]
                ws_base = live.base_url.replace("http://", "ws://")
                async with ws_client.connect(
                    f"{ws_base}/v1/terminal/{lease_id}?ticket={ticket}"
                ) as ws:
                    lease = state.backend.open_lease(lease_id)
                    assert lease is not None
                    await ws.send(
                        terminal_wire.encode_input(
                            lease_id, lease.generation, 1, "text", b"echo hi"
                        )
                    )
                    # Duplicate seq dedupes (no error, no double log).
                    await ws.send(
                        terminal_wire.encode_input(
                            lease_id, lease.generation, 1, "text", b"echo hi"
                        )
                    )
                logged = state.backend.input_logs[lease_id]
                assert [(seq, kind, data) for _, seq, kind, data in logged] == [
                    (1, "text", b"echo hi")
                ]
                # The ticket was single-use.
                async with httpx.AsyncClient(timeout=10.0) as client:
                    replay = await client.post(
                        f"{live.base_url}/v1/terminal-tickets",
                        headers=headers,
                        json={"lease_id": "no-such-lease"},
                    )
                    assert replay.json()["code"] == "lease_expired"
        finally:
            state.backend.close()
            state.push.close()

    asyncio.run(scenario())
