"""connector run-link over synthetic loopback.

Full enroll -> tunnel -> dispatch -> result loop against a live
gateway (JournalBackend, fixture adapter, tmp journals only); tamper
rejection, auth-failure exit, reconnect after restart, and CLI exit
codes. No cloud, no real daemon, no persistent services.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx
import pytest

from relaydeck.connector.cli import main as connector_main
from relaydeck.connector.enrollment import (
    claim_enrollment,
    generate_keys,
)
from relaydeck.connector.fixture import FIXTURE_EPOCH, FixtureAdapter, fixture_event
from relaydeck.connector.journal import ConnectorDispatch
from relaydeck.connector.link import ConnectorLink, LinkConfig, TunnelTransport
from relaydeck.contracts.schemas import GATEWAY_PROTOCOL
from relaydeck.gateway.app import GatewayState, create_app
from relaydeck.gateway.production import JournalBackend
from relaydeck.journal import CommandJournal, digest_for
from tests.support.live_server import LiveGateway
from tests.support.sync.scripted_transport import _ScriptedTransport

FIXTURES = Path(__file__).resolve().parent.parent / "fixtures" / "gateway"
NOW = 1_700_000_000_000
SECRET = b"connector-link-test-secret-32bytes"
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


def _seed(tmp_path: Path, name: str = "gw.db") -> tuple[GatewayState, JournalBackend]:
    journal = CommandJournal(tmp_path / name, clock=lambda: NOW)
    backend = JournalBackend(
        journal,
        now_ms=lambda: NOW,
        snapshots={"home-mac": json.loads((FIXTURES / "snapshot.json").read_text())},
    )
    state = GatewayState(backend=backend, cursor_secret=SECRET, now_ms=lambda: NOW)
    return state, backend


def _token(state: GatewayState) -> str:
    access, _ = state.sessions.create("acct-1", ALL_CAPS, frozenset({"home-mac"}), NOW)
    return access


def _envelope(command_id: str, host_epoch: str) -> dict[str, Any]:
    expires = datetime.fromtimestamp((NOW + 120_000) / 1000, UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    return {
        "protocol": GATEWAY_PROTOCOL,
        "command_id": command_id,
        "host_id": "home-mac",
        "host_epoch": host_epoch,
        "kind": "shell.create",
        "target": {"workspace_id": "w1"},
        "payload": {"label": "loop"},
        "expires_at": expires,
    }


def _dispatch_msg(
    command_id: str, kind: str, target: dict[str, Any], payload: dict[str, Any]
) -> dict[str, Any]:
    return {
        "kind": "command.dispatch",
        "command_id": command_id,
        "principal": "acct-1",
        "host_id": "home-mac",
        "gateway_kind": kind,
        "host_epoch": "epoch-fixture-1",
        "target": target,
        "payload": payload,
        "digest": digest_for("home-mac", kind, target, payload),
        "generation": 0,
        "expires_ms": 0,
    }


async def _enroll_live(base_url: str, user_token: str) -> tuple[str, str]:
    """Create an invitation and redeem it (real crypto, loopback HTTP)."""
    keys = generate_keys()
    async with httpx.AsyncClient(timeout=10.0) as client:
        created = await client.post(
            f"{base_url}/v1/enrollments",
            headers={"Authorization": f"Bearer {user_token}"},
            json={"host_label": "Home Mac", "pubkey": keys.public_openssh},
        )
        assert created.status_code == 201, created.text
        invitation = created.json()

        async def post_json(url: str, body: dict[str, Any]) -> tuple[int, dict[str, Any]]:
            response = await client.post(url, json=body)
            try:
                return response.status_code, response.json()
            except ValueError:
                return response.status_code, {}

        token, _ = await claim_enrollment(
            post_json, base_url, "home-mac", invitation["id"], invitation["code"], keys
        )
        return token, keys.fingerprint


async def _wait_for(predicate: Any, timeout_s: float = 30.0) -> None:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if await predicate():
            return
        await asyncio.sleep(0.05)
    raise AssertionError("timed out waiting for loopback condition")


def test_link_full_loop_enroll_push_dispatch_result(tmp_path: Path) -> None:
    async def scenario() -> None:
        state, backend = _seed(tmp_path)
        try:
            with LiveGateway(create_app(state)) as live:
                user_token = _token(state)
                host_token, _ = await _enroll_live(live.base_url, user_token)
                adapter = FixtureAdapter()
                link = ConnectorLink(
                    LinkConfig(
                        gateway_url=live.base_url,
                        host_id="home-mac",
                        host_token=host_token,
                        journal_path=str(tmp_path / "c.db"),
                        events=(
                            fixture_event("agent.upserted", "ev-link-1", resource_id="term-1"),
                        ),
                        clock=lambda: NOW,
                    ),
                    adapter,
                )
                stop = asyncio.Event()
                task = asyncio.create_task(link.run(stop))
                try:
                    async with httpx.AsyncClient(timeout=10.0) as client:
                        headers = {"Authorization": f"Bearer {user_token}"}

                        async def pushed() -> bool:
                            response = await client.get(
                                f"{live.base_url}/v1/hosts/home-mac/snapshot", headers=headers
                            )
                            return bool(response.json()["host"]["epoch"] == FIXTURE_EPOCH)

                        # The link's snapshot push lands before any command.
                        await _wait_for(pushed)
                        # Seed event is stored with a server-stamped cursor.
                        stored = backend.event_logs["home-mac"]
                        assert [e["event_id"] for e in stored] == ["ev-link-1"]
                        assert stored[0]["sequence"] == 1
                        assert stored[0]["cursor"]

                        submitted = await client.post(
                            f"{live.base_url}/v1/hosts/home-mac/commands",
                            headers=headers,
                            json=_envelope("link-001", FIXTURE_EPOCH),
                        )
                        assert submitted.status_code == 202, submitted.text

                        async def succeeded() -> bool:
                            response = await client.get(
                                f"{live.base_url}/v1/hosts/home-mac/commands/link-001",
                                headers=headers,
                            )
                            return bool(response.json()["status"] == "succeeded")

                        await _wait_for(succeeded)
                finally:
                    stop.set()
                    outcome = await asyncio.wait_for(task, timeout=15.0)
                assert outcome.reason == "shutdown"
                assert outcome.sessions == 1
                assert outcome.dispatches == 1
                assert outcome.results == 1
                assert outcome.pushed_snapshot is True
                assert outcome.pushed_events == 1
                # The fixture executed exactly one tab.create chain.
                assert [name for name, _ in adapter.calls] == ["tab.create"]
                assert adapter.prompts == ()
        finally:
            backend.close()

    asyncio.run(scenario())


def test_link_once_serves_single_dispatch_then_bye(tmp_path: Path) -> None:
    async def scenario() -> None:
        state, backend = _seed(tmp_path)
        # The command must target the epoch the connected host reports, or the connector
        # correctly refuses it as stale.
        backend.snapshots["home-mac"]["host"]["epoch"] = FIXTURE_EPOCH
        try:
            with LiveGateway(create_app(state)) as live:
                user_token = _token(state)
                host_token, _ = await _enroll_live(live.base_url, user_token)
                async with httpx.AsyncClient(timeout=10.0) as client:
                    headers = {"Authorization": f"Bearer {user_token}"}
                    submitted = await client.post(
                        f"{live.base_url}/v1/hosts/home-mac/commands",
                        headers=headers,
                        json=_envelope("link-once-1", FIXTURE_EPOCH),
                    )
                    assert submitted.status_code == 202, submitted.text
                adapter = FixtureAdapter()
                link = ConnectorLink(
                    LinkConfig(
                        gateway_url=live.base_url,
                        host_id="home-mac",
                        host_token=host_token,
                        journal_path=str(tmp_path / "c.db"),
                        once=True,
                        once_timeout_s=10.0,
                        clock=lambda: NOW,
                    ),
                    adapter,
                )
                outcome = await link.run(asyncio.Event())
                assert outcome.reason == "once-done"
                assert outcome.results == 1
                status = backend.command_status("link-once-1")
                assert status is not None
                assert status["status"] == "succeeded", status
                assert [name for name, _ in adapter.calls] == ["tab.create"]
        finally:
            backend.close()

    asyncio.run(scenario())


@contextlib.contextmanager
def _restart(app: Any, port: int) -> Any:
    last: Exception | None = None
    for _ in range(20):
        live = LiveGateway(app, port=port)
        try:
            live.__enter__()
            break
        except (OSError, RuntimeError) as exc:  # bind race
            last = exc
            time.sleep(0.2)
    else:
        raise RuntimeError(f"could not rebind {port}") from last
    try:
        yield live
    finally:
        live.__exit__(None, None, None)


def test_link_reconnects_after_gateway_restart(tmp_path: Path) -> None:
    async def scenario() -> None:
        state, backend = _seed(tmp_path)
        try:
            app = create_app(state)
            with LiveGateway(app) as first:
                port = first.port
                user_token = _token(state)
                host_token, _ = await _enroll_live(first.base_url, user_token)
                adapter = FixtureAdapter()
                link = ConnectorLink(
                    LinkConfig(
                        gateway_url=f"http://127.0.0.1:{port}",
                        host_id="home-mac",
                        host_token=host_token,
                        journal_path=str(tmp_path / "c.db"),
                        backoff_seed=7,
                        backoff_base_ms=50.0,
                        backoff_max_ms=200.0,
                        clock=lambda: NOW,
                    ),
                    adapter,
                )
                stop = asyncio.Event()
                task = asyncio.create_task(link.run(stop))
                try:
                    async with httpx.AsyncClient(timeout=10.0) as client:
                        headers = {"Authorization": f"Bearer {user_token}"}

                        async def pushed(base: str) -> bool:
                            try:
                                response = await client.get(
                                    f"{base}/v1/hosts/home-mac/snapshot", headers=headers
                                )
                            except httpx.HTTPError:
                                return False
                            return (
                                response.status_code == 200
                                and response.json()["host"]["epoch"] == FIXTURE_EPOCH
                            )

                        await _wait_for(lambda: pushed(first.base_url))
                    # Restart the server (same app/state: enrollment survives).
                    first.__exit__(None, None, None)
                    with _restart(app, port) as live:
                        assert live.port == port
                        async with httpx.AsyncClient(timeout=10.0) as client:
                            headers = {"Authorization": f"Bearer {user_token}"}
                            await _wait_for(lambda: pushed(live.base_url), timeout_s=25.0)
                            submitted = await client.post(
                                f"{live.base_url}/v1/hosts/home-mac/commands",
                                headers=headers,
                                json=_envelope("link-re-1", FIXTURE_EPOCH),
                            )
                            assert submitted.status_code == 202, submitted.text

                            async def succeeded() -> bool:
                                response = await client.get(
                                    f"{live.base_url}/v1/hosts/home-mac/commands/link-re-1",
                                    headers=headers,
                                )
                                return bool(response.json()["status"] == "succeeded")

                            await _wait_for(succeeded)
                finally:
                    stop.set()
                    outcome = await asyncio.wait_for(task, timeout=15.0)
                assert outcome.reason == "shutdown"
                assert outcome.sessions >= 2
                assert outcome.results == 1
        finally:
            backend.close()

    asyncio.run(scenario())


def test_link_drops_tampered_dispatch_unacked(tmp_path: Path) -> None:
    async def scenario() -> None:
        tampered = _dispatch_msg("tx-1", "shell.create", {"workspace_id": "w1"}, {})
        tampered["payload"] = {"label": "evil"}
        script = _ScriptedTransport(
            [
                json.dumps({"kind": "hello", "host_id": "home-mac"}),
                json.dumps(tampered),
            ]
        )

        async def opener() -> TunnelTransport:
            return script

        adapter = FixtureAdapter()
        link = ConnectorLink(
            LinkConfig(
                gateway_url="http://127.0.0.1:9",
                host_id="home-mac",
                host_token="rdh_dead",
                journal_path=str(tmp_path / "c.db"),
                max_retries=0,
                ping_interval_s=0,
                clock=lambda: NOW,
            ),
            adapter,
            opener=opener,
        )
        outcome = await link.run(asyncio.Event())
        assert outcome.reason == "retries-exhausted"
        assert outcome.sessions == 1
        assert outcome.dispatches == 0
        assert outcome.results == 0
        # Snapshot push only: no ack, no result, no Herdr call.
        kinds = [json.loads(text)["kind"] for text in script.sent]
        assert kinds == ["snapshot"]
        assert adapter.calls == ()
        assert script.closed

    asyncio.run(scenario())


def test_link_auth_failure_exits_without_retry(tmp_path: Path) -> None:
    async def scenario() -> None:
        state, backend = _seed(tmp_path)
        try:
            with LiveGateway(create_app(state)) as live:
                link = ConnectorLink(
                    LinkConfig(
                        gateway_url=live.base_url,
                        host_id="home-mac",
                        host_token="rdh_bogus",
                        journal_path=str(tmp_path / "c.db"),
                        max_retries=5,
                        clock=lambda: NOW,
                    ),
                    FixtureAdapter(),
                )
                outcome = await link.run(asyncio.Event())
                assert outcome.reason == "auth-failed"
                assert outcome.sessions == 0
        finally:
            backend.close()

    asyncio.run(scenario())


def test_link_backoff_is_seeded_and_bounded(tmp_path: Path) -> None:
    def delays(seed: int) -> list[float]:
        link = ConnectorLink(
            LinkConfig(
                gateway_url="http://127.0.0.1:9",
                host_id="h",
                host_token="t",
                journal_path=str(tmp_path / f"c-{seed}.db"),
                backoff_seed=seed,
            ),
            FixtureAdapter(),
        )
        try:
            return [link._backoff_ms(attempt) for attempt in range(10)]
        finally:
            link._journal.close()

    first, second = delays(11), delays(11)
    assert first == second
    assert delays(12) != first
    assert all(0.0 <= delay <= 30_000 * 1.25 for delay in first)
    assert first[9] >= first[0]


def test_fixture_executes_all_dispatch_chains(tmp_path: Path) -> None:
    async def scenario() -> None:
        journal = CommandJournal(tmp_path / "c.db", clock=lambda: NOW)
        try:
            adapter = FixtureAdapter()
            dispatcher = ConnectorDispatch(journal, adapter, host_id="home-mac", clock=lambda: NOW)
            shell = await dispatcher.dispatch(
                _dispatch_msg("fx-1", "shell.create", {"workspace_id": "w1"}, {"label": "ops"})
            )
            assert shell["status"] == "succeeded"
            assert shell["result"]["type"] == "tab_created"
            pane_id = shell["result"]["pane_id"]
            started = await dispatcher.dispatch(
                _dispatch_msg(
                    "fx-2",
                    "agent.start",
                    {"workspace_id": "w1"},
                    {"kind": "agent-a", "name": "web"},
                )
            )
            assert started["status"] == "succeeded"
            agent_id = started["result"]["agent_id"]
            prompted = await dispatcher.dispatch(
                _dispatch_msg("fx-3", "agent.prompt", {"agent_id": agent_id}, {"text": "hi"})
            )
            assert prompted["status"] == "succeeded"
            assert adapter.prompts == ((agent_id, "hi"),)
            closed = await dispatcher.dispatch(
                _dispatch_msg("fx-4", "session.close", {"terminal_id": pane_id}, {})
            )
            assert closed["status"] == "succeeded"
            # Unknown gateway kind is rejected, never executed.
            calls_before = len(adapter.calls)
            rejected = await dispatcher.dispatch(
                _dispatch_msg("fx-5", "workspace.create", {"x": "y"}, {})
            )
            assert rejected["status"] == "rejected"
            assert len(adapter.calls) == calls_before
            # Unknown agent proves non-delivery: failed, not unknown.
            missing = await dispatcher.dispatch(
                _dispatch_msg("fx-6", "agent.prompt", {"agent_id": "ghost"}, {"text": "hi"})
            )
            assert missing["status"] == "failed"
            assert missing["error_code"] == "no_such_agent"
            # Same ID + changed body: conflict outcome, zero new calls.
            conflict = await dispatcher.dispatch(
                _dispatch_msg("fx-1", "shell.create", {"workspace_id": "w1"}, {"label": "other"})
            )
            assert conflict["command_id"] == "fx-1"
            assert len(adapter.calls) == calls_before + 1  # only fx-6's refused prompt
        finally:
            journal.close()

    asyncio.run(scenario())


def test_cli_run_exit_codes(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    assert connector_main(["run"]) == 2  # real Herdr refused
    assert connector_main(["run", "--fixture", "--once"]) == 1  # not enrolled
    assert (
        connector_main(["run", "--fixture", "--once", "--log-config", str(tmp_path / "no.ini")])
        == 2
    )
