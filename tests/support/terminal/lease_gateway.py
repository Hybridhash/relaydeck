"""Gateway attach/lease helpers for terminal lease tests."""

from __future__ import annotations

import json
from typing import Any

from fastapi.testclient import TestClient

from relaydeck.gateway.app import GatewayState, create_app
from relaydeck.gateway.backend import FakeBackend
from tests.support.paths import TESTS_DIR

FIXTURES = TESTS_DIR / "fixtures" / "gateway"


SECRET = b"integration-test-secret-32bytes!!"


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


HOSTS = frozenset({"home-mac"})


NOW = 1_000_000


class _Clock:
    def __init__(self, now: int = NOW) -> None:
        self.now = now

    def __call__(self) -> int:
        return self.now


def _client() -> tuple[TestClient, GatewayState, FakeBackend, _Clock]:
    clock = _Clock()
    backend = FakeBackend(now_ms=clock)
    doc: dict[str, Any] = json.loads((FIXTURES / "snapshot.json").read_text())
    doc["cursor"] = "snap-cursor-1"
    backend.snapshots["home-mac"] = doc
    state = GatewayState(backend=backend, cursor_secret=SECRET, now_ms=clock)
    return TestClient(create_app(state)), state, backend, clock


def _token(
    state: GatewayState,
    account: str = "acct-1",
    device: str = "dev-1",
    caps: frozenset[str] = ALL_CAPS,
    now: int = NOW,
) -> str:
    access, _ = state.sessions.create(account, caps, HOSTS, now, device)
    return access


def _headers(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def _attach(
    client: TestClient,
    token: str,
    terminal: str = "term-1",
    kind: str = "shell",
    takeover: bool = False,
) -> Any:
    return client.post(
        f"/v1/hosts/home-mac/terminals/{terminal}/attachments",
        headers=_headers(token),
        json={"kind": kind, "takeover": takeover, "cols": 80, "rows": 24},
    )


def _lease(client: TestClient, token: str, attachment_id: str, mode: str) -> Any:
    return client.post(
        f"/v1/terminal-attachments/{attachment_id}/leases",
        headers=_headers(token),
        json={"mode": mode},
    )


def _ticket(client: TestClient, token: str, lease_id: str) -> str:
    r = client.post("/v1/terminal-tickets", headers=_headers(token), json={"lease_id": lease_id})
    assert r.status_code == 200, r.text
    ticket: str = r.json()["ticket"]
    return ticket
