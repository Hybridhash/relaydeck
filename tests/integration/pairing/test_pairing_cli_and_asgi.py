"""Exact local CLI and real ASGI contract, temporary synthetic state only."""

from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
from typing import Any

from fastapi.testclient import TestClient

from relaydeck.companion.cli import main
from relaydeck.companion.config import load_config
from relaydeck.companion.runtime import CompanionRuntime
from relaydeck.contracts.schemas import validate
from relaydeck.gateway.app import create_app
from relaydeck.gateway.revocation import revoke_host_and_close
from tests.security.helpers import auth
from tests.support.companion.asgi_companion import AsgiServer, config_file
from tests.support.pairing import ORIGIN, VERIFIER, foundation, proof


def test_exact_companion_cli_approval_and_denial(
    tmp_path: Path, capsys: Any, monkeypatch: Any
) -> None:
    config = config_file(tmp_path, advertised_origin=ORIGIN)
    state, now, body = foundation(tmp_path / "state")
    monkeypatch.setattr("relaydeck.companion.pairing.time.time", lambda: now[0] / 1000)
    argv = ["--config", str(config), "pairing"]
    assert main([*argv, "open", "--ttl-seconds", "120"]) == 0
    assert "selected host only" in capsys.readouterr().out
    client = TestClient(create_app(state))
    row = client.post("/v1/pairing/requests", json={**body, "device_label": "Phone\u202e"}).json()
    validate("relaydeck/gateway/v1/pairing-status", row)
    assert main([*argv, "list"]) == 0
    output = capsys.readouterr().out
    assert row["matching_code"] in output and "Scope:" in output
    assert "\\u202e" in output and "\u202e" not in output
    assert VERIFIER not in output
    assert (
        main([*argv, "approve", "--request", row["request_id"], "--code", row["matching_code"]])
        == 0
    )
    assert "Device request approved" in capsys.readouterr().out
    response = client.post(f"/v1/pairing/{row['request_id']}/redeem", json=proof())
    assert response.status_code == 200, response.text
    validate("relaydeck/gateway/v1/token-response", response.json())
    other = client.post("/v1/pairing/requests", json=body).json()
    assert other["device_id"] != row["device_id"]
    assert (
        main([*argv, "deny", "--request", other["request_id"], "--code", other["matching_code"]])
        == 0
    )
    assert (
        client.post(f"/v1/pairing/{other['request_id']}/status", json=proof()).json()["state"]
        == "denied"
    )
    assert main([*argv, "close"]) == 0
    assert client.post("/v1/pairing/requests", json=body).status_code == 404
    text = (tmp_path / "state" / "pairing.json").read_text()
    assert VERIFIER not in text and response.json()["access_token"] not in text
    assert os.stat(tmp_path / "state" / "pairing.json").st_mode & 0o777 == 0o600


def test_cli_missing_enrollment_has_truthful_guidance(tmp_path: Path, capsys: Any) -> None:
    path = config_file(tmp_path)
    assert main(["--config", str(path), "pairing", "open"]) == 2
    assert "Enroll this Mac's connector locally" in capsys.readouterr().out


def test_companion_wires_pairing_without_identity_and_restart_discards_it(tmp_path: Path) -> None:
    async def scenario() -> None:
        config = load_config(config_file(tmp_path, advertised_origin=ORIGIN))
        runtime = CompanionRuntime(config, server_factory=AsgiServer)
        await runtime.start(asyncio.Event())
        try:
            assert runtime.gateway.identity_provider is None
            store = runtime.gateway.pairing
            assert store is not None
            assert runtime.report()["pairing_available"]
            assert json.loads(store.path.read_text())["window"] is None
            # Deliberately no enrollment/Herdr connection in this fixture.
            assert not runtime.report()["connector_ready"]
        finally:
            await runtime.close()
        # The real factory used by the companion resets any persisted approval.
        data = json.loads(store.path.read_text())
        old_boot = data["boot"]
        runtime = CompanionRuntime(config, server_factory=AsgiServer)
        await runtime.start(asyncio.Event())
        try:
            assert json.loads(store.path.read_text())["boot"] != old_boot
            assert json.loads(store.path.read_text())["window"] is None
        finally:
            await runtime.close()

    asyncio.run(scenario())


def test_host_revoke_consumes_paired_sessions_and_refresh(tmp_path: Path) -> None:
    state, now, body = foundation(tmp_path / "state")
    store = state.pairing
    assert store is not None
    host = state.hosts.record_for_host("home-mac")
    store.open_window(host, ORIGIN, now[0], 120)
    client = TestClient(create_app(state))
    row = client.post("/v1/pairing/requests", json=body).json()
    store.decide(row["request_id"], row["matching_code"], host, ORIGIN, now[0], approve=True)
    tokens = client.post(f"/v1/pairing/{row['request_id']}/redeem", json=proof()).json()
    assert asyncio.run(revoke_host_and_close(state, "home-mac"))[0]
    assert client.get("/v1/me", headers=auth(tokens["access_token"])).status_code == 401
    assert not state.sessions._by_access
    assert (
        client.post(
            "/v1/token", json={"grant": "refresh_token", "refresh_token": tokens["refresh_token"]}
        ).status_code
        == 401
    )
