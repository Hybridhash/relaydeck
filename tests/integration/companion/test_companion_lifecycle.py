"""Real lifecycle/files/ASGI, explicit server/Herdr doubles; no TCP or enrollment."""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

import httpx
import pytest

from relaydeck.companion.cli import main, probe_private
from relaydeck.companion.config import load_config
from relaydeck.companion.lifecycle import (
    AlreadyRunning,
    StateLease,
    request_stop,
    status,
    supervise,
)
from relaydeck.companion.private import read_private, write_json
from relaydeck.companion.runtime import CompanionRuntime
from relaydeck.connector.enrollment import ConnectorCreds, save_creds
from relaydeck.connector.fixture import FixtureAdapter
from relaydeck.connector.herdr import HerdrProtocolError
from relaydeck.contracts.endpoints import origin
from relaydeck.gateway.hosts import HostStore
from relaydeck.gateway.identity import pkce_challenge
from tests.security.helpers import ALL_CAPS, auth
from tests.support.asgi_socket import AsgiSocket
from tests.support.companion.asgi_companion import AsgiServer, ProcessResources, config_file


def test_foreground_cli_duplicate_stop_restart_and_unrelated_survival(tmp_path: Path) -> None:
    config = config_file(tmp_path)
    state = load_config(config).state_dir
    script = (
        "import relaydeck.companion.cli as cli; "
        "from tests.support.companion.asgi_companion import ProcessResources; "
        "cli.CompanionRuntime=ProcessResources; raise SystemExit(cli.main())"
    )
    args = [sys.executable, "-c", script, "--config", str(config), "start"]
    unrelated = subprocess.Popen(
        [sys.executable, "-c", "import sys; sys.stdin.read()"], stdin=subprocess.PIPE
    )
    child = None
    transcript: list[str] = []
    try:
        for iteration in (1, 2):
            child = subprocess.Popen(
                args, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True
            )
            deadline = time.monotonic() + 8
            while time.monotonic() < deadline:
                report = status(state)
                if report["state"] == "ready":
                    break
                assert child.poll() is None
                time.sleep(0.02)
            assert report["state"] == "ready"
            transcript.append(f"start {iteration}: {report['state']} (synthetic resources)")
            duplicate = subprocess.run(args, capture_output=True, text=True, timeout=5)
            assert duplicate.returncode == 2 and "already owns" in duplicate.stdout
            transcript.append("duplicate start: rejected by held state lock")
            assert unrelated.poll() is None
            stop = subprocess.run(
                [sys.executable, "-m", "relaydeck.companion.cli", "--config", str(config), "stop"],
                capture_output=True,
                text=True,
                timeout=6,
            )
            assert stop.returncode == 0, stop.stdout
            stdout, stderr = child.communicate(timeout=5)
            assert child.returncode == 0, stderr
            transcript.append(stop.stdout.strip())
            assert "Foreground companion:" in stdout
            assert status(state) == {"state": "stopped"}
            assert unrelated.poll() is None
            assert json.loads(read_private(state / "synthetic-starts.json"))["count"] == iteration
        print("\n".join(transcript))
    finally:
        if child is not None and child.poll() is None:
            asyncio.run(request_stop(state, timeout=3))
            child.communicate(timeout=5)
        assert unrelated.stdin is not None
        unrelated.stdin.close()
        unrelated.wait(timeout=5)


def test_partial_start_cleanup_and_nonce_cannot_stop_replacement(tmp_path: Path) -> None:
    config = load_config(config_file(tmp_path))
    broken = ProcessResources(config)

    async def fail(stop: asyncio.Event) -> None:
        await ProcessResources.start(broken, stop)
        raise RuntimeError("synthetic partial-start failure")

    broken.start = fail  # type: ignore[method-assign]

    async def scenario() -> None:
        with pytest.raises(RuntimeError, match="partial-start"):
            await supervise(config.state_dir, lambda: broken, asyncio.Event())
        assert broken.closed and status(config.state_dir)["state"] == "stopped"
        with StateLease(config.state_dir) as replacement:
            replacement.publish("ready", synthetic=True)
            write_json(config.state_dir / "companion-stop.json", {"instance": "previous-instance"})
            assert not replacement.stop_requested()
            with pytest.raises(AlreadyRunning):
                with StateLease(config.state_dir):
                    pytest.fail("duplicate owner acquired")

    asyncio.run(scenario())


def test_stale_status_never_kills_a_pid(tmp_path: Path) -> None:
    with StateLease(tmp_path) as lease:
        lease.publish("ready")
        body = json.loads(read_private(tmp_path / "companion-status.json"))
        body.update(heartbeat=time.time() - 60, pid=os.getpid())
        write_json(tmp_path / "companion-status.json", body)
        assert status(tmp_path)["state"] == "unresponsive"
        assert not asyncio.run(request_stop(tmp_path, timeout=0.1))
        assert lease.stop_requested()


@pytest.mark.parametrize(
    "value,options",
    [
        ("http://192.168.1.4:8125", {"loopback": True}),
        ("http://localhost:8125", {"loopback": True}),
        ("http://test-mac.tailnet.ts.net", {"tailscale": True}),
        ("https://public.example.com", {"tailscale": True}),
        ("https://user:secret@100.64.0.1", {"tailscale": True}),
        ("https://test-mac.tailnet.ts.net/auth", {"tailscale": True}),
        ("https://test-mac.tailnet.ts.net?token=value", {"tailscale": True}),
        ("https://test-mac.tailnet.ts.net:70000", {"tailscale": True}),
    ],
)
def test_endpoint_rejects_plaintext_lan_public_or_embedded_credentials(
    value: str,
    options: dict[str, bool],
) -> None:
    with pytest.raises(ValueError):
        origin(value, **options)


def test_private_config_no_secret_env_fallback_or_symlink(tmp_path: Path, monkeypatch: Any) -> None:
    monkeypatch.setenv("RELAYDECK_COOKIE_SECRET", "environment-cannot-bypass-files" * 3)
    target = config_file(tmp_path)
    config = load_config(target)
    assert config.local_endpoint == "http://127.0.0.1:8125"
    assert config.advertised_origin == "https://test-mac.synthetic-tailnet.ts.net"
    assert config.secrets()[0] != config.secrets()[1]
    config.cookie_secret_file.chmod(0o644)
    with pytest.raises(ValueError, match="owner-only"):
        config.secrets()
    config.cookie_secret_file.unlink()
    config.cookie_secret_file.symlink_to(config.cursor_secret_file)
    with pytest.raises(ValueError, match="owner-only"):
        config.secrets()
    config.cookie_secret_file.unlink()
    with pytest.raises(FileNotFoundError):
        config.secrets()


def test_durable_enrollment_rotation_revocation_and_corruption_fail_closed(tmp_path: Path) -> None:
    path = tmp_path / "hosts.json"
    store = HostStore(path=path)
    token = store.enroll("home-mac", account="owner")
    assert token not in read_private(path)
    restored = HostStore(path=path)
    assert restored.lookup(token) is not None
    rotated = restored.enroll("home-mac", account="owner")
    again = HostStore(path=path)
    assert again.lookup(token) is None and again.lookup(rotated) is not None
    assert again.revoke("home-mac")
    assert HostStore(path=path).lookup(rotated) is None
    path.write_text('{"version":1,"hosts":[{}]}')
    with pytest.raises(ValueError):
        HostStore(path=path)


async def wait_until(predicate: Any) -> None:
    async with asyncio.timeout(3):
        while not predicate():
            await asyncio.sleep(0.02)


def test_combined_app_local_connector_private_origin_auth_and_restart(
    tmp_path: Path,
    monkeypatch: Any,
) -> None:
    import flet.fastapi as flet_fastapi

    starts: list[str] = []

    async def flet_start() -> None:
        starts.append("start")

    async def flet_stop() -> None:
        starts.append("stop")

    monkeypatch.setattr(flet_fastapi.app_manager, "start", flet_start)
    monkeypatch.setattr(flet_fastapi.app_manager, "shutdown", flet_stop)
    config = load_config(config_file(tmp_path))
    config.state_dir.mkdir(mode=0o700)
    host_store = HostStore(path=config.state_dir / "hosts.json")
    token = host_store.enroll(config.host_id, account="owner")
    save_creds(
        ConnectorCreds(config.advertised_origin, config.host_id, token),
        str(config.connector_credentials),
    )

    async def scenario() -> None:
        runtime = CompanionRuntime(config, fixture=True, server_factory=AsgiServer)

        async def open_tunnel() -> Any:
            return await AsgiSocket(runtime.app, "/v1/connectors", token=token).open()

        runtime.opener = open_tunnel
        stop = asyncio.Event()
        try:
            await runtime.start(stop)
            await wait_until(lambda: runtime.report()["connector_ready"])
            assert runtime.report()["identity_ready"] is False
            assert runtime.report()["state"] == "setup_required"
            assert runtime.gateway.backend.requires_connector
            transport = httpx.ASGITransport(app=runtime.app)
            async with httpx.AsyncClient(
                transport=transport, base_url=config.advertised_origin
            ) as http:
                assert (await http.get("/healthz")).json()["ok"] is True
                assert (await http.get("/v1/me")).status_code == 401
                access, refresh = runtime.gateway.sessions.create(
                    "owner",
                    ALL_CAPS,
                    frozenset({config.host_id}),
                    runtime.gateway.now_ms(),
                )
                assert (await http.get("/v1/me", headers=auth(access))).status_code == 200
                assert (await http.get("/v1/auth/dev-login")).status_code != 302
                # Seeded PKCE is an explicit synthetic identity proof, not provider integration.
                state, verifier = "synthetic-state", "synthetic-verifier-" + "v" * 43
                assert runtime.web_store is not None
                runtime.web_store._pending[state] = (verifier, runtime.gateway.now_ms() + 10_000)
                runtime.gateway.pending_codes["synthetic-code"] = {
                    "account": "owner",
                    "capabilities": list(ALL_CAPS),
                    "hosts": [config.host_id],
                    "challenge": pkce_challenge(verifier),
                }
                response = await http.get(
                    "/v1/auth/callback",
                    params={
                        "state": state,
                        "code": "synthetic-code",
                    },
                )
                assert response.status_code == 302, response.text
                cookie = response.headers["set-cookie"]
                assert "Secure" in cookie and "HttpOnly" in cookie and "SameSite=Lax" in cookie
                assert (
                    await http.post(
                        "/v1/auth/logout",
                        headers={
                            "Origin": config.local_endpoint,
                        },
                    )
                ).status_code == 403
                assert (
                    await http.post(
                        "/v1/auth/logout",
                        headers={
                            "Origin": config.advertised_origin,
                        },
                    )
                ).status_code == 204
                assert (await http.get("/v1/me", headers=auth(access))).status_code == 200
                assert refresh not in read_private(config.state_dir / "hosts.json")
        finally:
            await runtime.close()
        assert runtime.server.closed and runtime.gateway.sockets.host_count(config.host_id) == 0
        assert runtime.source.calls == ()  # No Herdr close/agent command was dispatched.
        assert starts == ["start", "stop"]
        # Same enrolled connector remains valid; browser sessions/leases do not survive restart.
        second = CompanionRuntime(config, server_factory=AsgiServer)
        try:
            await second.start(asyncio.Event())
            assert second.gateway.hosts.lookup(token) is not None
            assert second.gateway.sessions.lookup(access, second.gateway.now_ms()) is None
            assert second.gateway.backend.epochs_for_host(config.host_id, "owner") == []
        finally:
            await second.close()
        assert starts == ["start", "stop", "start", "stop"]

    asyncio.run(scenario())


def test_runtime_partial_source_failure_cleans_owned_server(
    tmp_path: Path, monkeypatch: Any
) -> None:
    import flet.fastapi as flet_fastapi

    async def nothing() -> None:
        pass

    monkeypatch.setattr(flet_fastapi.app_manager, "start", nothing)
    monkeypatch.setattr(flet_fastapi.app_manager, "shutdown", nothing)
    config = load_config(config_file(tmp_path))
    config.state_dir.mkdir(mode=0o700)
    token = HostStore(path=config.state_dir / "hosts.json").enroll("home-mac", account="owner")
    save_creds(
        ConnectorCreds(config.advertised_origin, config.host_id, token),
        str(config.connector_credentials),
    )

    class BrokenSource(FixtureAdapter):
        closed = False

        async def bootstrap(self) -> None:
            raise HerdrProtocolError("protocol_mismatch", "synthetic incompatible daemon")

        async def close(self) -> None:
            self.closed = True

    async def scenario() -> None:
        runtime = CompanionRuntime(
            config, connect_herdr=True, server_factory=AsgiServer, source_factory=BrokenSource
        )
        try:
            await runtime.start(asyncio.Event())
            assert runtime.report()["state"] == "setup_required"
            assert "offline or incompatible" in runtime.issue
            assert runtime.link_task is None
        finally:
            await runtime.close()
        assert runtime.source.closed and runtime.server.closed
        assert HostStore(path=config.state_dir / "hosts.json").lookup(token) is not None

    asyncio.run(scenario())


def test_private_route_diagnostic_never_bypasses_tls(monkeypatch: Any) -> None:
    seen: dict[str, Any] = {}

    class Unreachable:
        def __init__(self, **kwargs: Any) -> None:
            seen.update(kwargs)

        async def __aenter__(self) -> Unreachable:
            return self

        async def __aexit__(self, *args: Any) -> None:
            pass

        async def get(self, url: str) -> Any:
            raise httpx.ConnectError("synthetic unreachable route")

    monkeypatch.setattr(httpx, "AsyncClient", Unreachable)
    result = asyncio.run(probe_private("https://test-mac.synthetic-tailnet.ts.net"))
    assert result["state"] == "failed" and "Tailscale" in result["message"]
    assert "verify" not in seen and seen["trust_env"] is False


def test_doctor_reports_setup_without_starting_services(tmp_path: Path, capsys: Any) -> None:
    config = config_file(tmp_path)
    assert main(["--config", str(config), "doctor"]) == 0
    report = json.loads(capsys.readouterr().out)
    assert report["private_route"]["state"] == "unverified"
    assert "No separate account login" in report["identity"]
    assert not load_config(config).state_dir.exists()


def test_stop_during_partial_start_cancels_only_owned_resources(tmp_path: Path) -> None:
    config = load_config(config_file(tmp_path))
    held = ProcessResources(config)
    started = asyncio.Event()

    async def slow_start(stop: asyncio.Event) -> None:
        started.set()
        await asyncio.Event().wait()

    held.start = slow_start  # type: ignore[method-assign]

    async def scenario() -> None:
        owner = asyncio.create_task(supervise(config.state_dir, lambda: held, asyncio.Event()))
        await started.wait()
        assert status(config.state_dir)["state"] == "starting"
        assert await request_stop(config.state_dir, timeout=2)
        assert await owner == 0
        assert held.closed and status(config.state_dir)["state"] == "stopped"

    asyncio.run(scenario())


def test_occupied_listener_failure_preserves_durable_state(tmp_path: Path) -> None:
    config = load_config(config_file(tmp_path))

    class RejectedServer(AsgiServer):
        async def serve(self) -> None:
            raise SystemExit(1)  # Actual Uvicorn bind-failure behavior, no real socket.

    async def scenario() -> None:
        runtime = CompanionRuntime(config, server_factory=RejectedServer)
        with pytest.raises(RuntimeError, match="preserve the existing listener"):
            await supervise(config.state_dir, lambda: runtime, asyncio.Event())
        assert runtime.server_task is not None and runtime.server_task.done()
        assert (config.state_dir / "gateway-journal.db").exists()
        assert (config.state_dir / "push.db").exists()
        assert status(config.state_dir)["state"] == "stopped"

    asyncio.run(scenario())


def test_bridge_only_companion_serves_api_and_connector_without_web_ui(
    tmp_path: Path, monkeypatch: Any
) -> None:
    """web_ui = false: the Android bridge. Gateway API (health, auth, pairing)
    and the Herdr connector run; no browser UI, web session or Flet runtime."""
    import flet.fastapi as flet_fastapi

    starts: list[str] = []

    async def flet_start() -> None:
        starts.append("start")

    monkeypatch.setattr(flet_fastapi.app_manager, "start", flet_start)
    config = load_config(config_file(tmp_path, web_ui=False))
    assert config.web_ui is False
    config.state_dir.mkdir(mode=0o700)
    token = HostStore(path=config.state_dir / "hosts.json").enroll(config.host_id, account="owner")
    save_creds(
        ConnectorCreds(config.advertised_origin, config.host_id, token),
        str(config.connector_credentials),
    )

    async def scenario() -> None:
        runtime = CompanionRuntime(config, fixture=True, server_factory=AsgiServer)

        async def open_tunnel() -> Any:
            return await AsgiSocket(runtime.app, "/v1/connectors", token=token).open()

        runtime.opener = open_tunnel
        try:
            await runtime.start(asyncio.Event())
            await wait_until(lambda: runtime.report()["connector_ready"])
            report = runtime.report()
            assert report["web_ui"] is False and report["web_ready"] is False
            assert report["api_ready"] is True
            assert runtime.web_store is None and starts == []
            transport = httpx.ASGITransport(app=runtime.app)
            async with httpx.AsyncClient(
                transport=transport, base_url=config.advertised_origin
            ) as http:
                assert (await http.get("/healthz")).json()["ok"] is True
                assert (await http.get("/v1/me")).status_code == 401
                # No browser UI is mounted at the root.
                assert (await http.get("/")).status_code == 404
                # Android pairing endpoints stay available.
                pairing = await http.post("/v1/pairing/requests", json={})
                assert pairing.status_code != 404
        finally:
            await runtime.close()

    asyncio.run(scenario())


def test_web_ui_setting_must_be_boolean(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="web_ui"):
        load_config(config_file(tmp_path, web_ui="no"))
    assert load_config(config_file(tmp_path)).web_ui is True
