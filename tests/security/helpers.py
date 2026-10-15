"""Shared attack-test builders. Localhost only.

Every helper builds a real gateway app over FakeBackend (or the real
stores) behind Starlette TestClient. No network leaves loopback; no
live daemon is touched.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from fastapi.testclient import TestClient

from relaydeck.gateway.app import GatewayState, create_app
from relaydeck.gateway.backend import FakeBackend

FIXTURES = Path(__file__).resolve().parent.parent / "fixtures" / "gateway"
SECRET = b"task-024-security-test-secret-32b!!"
NOW = 1_000_000

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


def snapshot_doc(cursor: str = "sec-cursor-1") -> dict[str, Any]:
    doc: dict[str, Any] = json.loads((FIXTURES / "snapshot.json").read_text())
    doc["cursor"] = cursor
    return doc


def make_state(
    now: int = NOW,
    hosts: tuple[str, ...] = ("home-mac",),
) -> tuple[GatewayState, FakeBackend]:
    backend = FakeBackend(now_ms=lambda: now)
    for host in hosts:
        backend.snapshots[host] = snapshot_doc(f"sec-cursor-{host}")
    state = GatewayState(backend=backend, cursor_secret=SECRET, now_ms=lambda: now)
    return state, backend


def make_client(
    now: int = NOW,
    hosts: tuple[str, ...] = ("home-mac",),
) -> tuple[TestClient, GatewayState, FakeBackend]:
    state, backend = make_state(now, hosts)
    return TestClient(create_app(state)), state, backend


def mint_token(
    state: GatewayState,
    account: str = "acct-1",
    caps: frozenset[str] = ALL_CAPS,
    hosts: frozenset[str] = frozenset({"home-mac"}),
    now: int = NOW,
) -> str:
    access, _ = state.sessions.create(account, caps, hosts, now)
    return access


def auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def command_body(
    command_id: str = "cmd-sec-001",
    host_id: str = "home-mac",
    kind: str = "agent.prompt",
    text: str = "hello",
) -> dict[str, Any]:
    epoch = "epoch-7"
    return {
        "protocol": "relaydeck.gateway.v1",
        "command_id": command_id,
        "host_id": host_id,
        "host_epoch": epoch,
        "kind": kind,
        "target": {"agent_id": "term-1"},
        "payload": {"text": text},
    }


def attach(
    client: TestClient,
    token: str,
    terminal_id: str = "term-1",
    host_id: str = "home-mac",
    kind: str = "shell",
    takeover: bool = False,
) -> dict[str, Any]:
    r = client.post(
        f"/v1/hosts/{host_id}/terminals/{terminal_id}/attachments",
        headers=auth(token),
        json={"kind": kind, "takeover": takeover},
    )
    assert r.status_code in (200, 201), r.text
    body: dict[str, Any] = r.json()
    return body


def lease(
    client: TestClient,
    token: str,
    attachment_id: str,
    mode: str = "control",
) -> dict[str, Any]:
    r = client.post(
        f"/v1/terminal-attachments/{attachment_id}/leases",
        headers=auth(token),
        json={"mode": mode},
    )
    assert r.status_code == 201, r.text
    out: dict[str, Any] = r.json()
    return out


def ticket(client: TestClient, token: str, lease_id: str) -> str:
    r = client.post(
        "/v1/terminal-tickets",
        headers=auth(token),
        json={"lease_id": lease_id},
    )
    assert r.status_code == 200, r.text
    return str(r.json()["ticket"])
