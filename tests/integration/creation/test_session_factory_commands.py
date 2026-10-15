"""Actual SessionFactory -> HTTP/journal/tunnel/source; synthetic PTYs only."""

from __future__ import annotations

import asyncio
import time
from pathlib import Path
from typing import Any

import httpx
import pytest

from relaydeck.client.features.composer.domain import (
    SEND_DELIVERED_VIA_ATTACH,
    SEND_DELIVERED_VIA_PROMPT,
    SEND_FAILED,
    SEND_IGNORED,
    SEND_UNKNOWN,
    ComposerStore,
)
from relaydeck.client.features.composer.observation import CommandObserver, CommandOutcomeUnknown
from relaydeck.client.features.shell.ops import HttpxShellGatewayOps
from relaydeck.client.features.sync.transport import (
    HttpxGatewayTransport,
    TransportError,
)
from relaydeck.client.sessions import SessionFactory
from relaydeck.connector.fixture import FixtureAdapter
from relaydeck.connector.herdr import HerdrAdapter, HerdrError
from relaydeck.connector.link import ConnectorLink, LinkConfig
from relaydeck.connector.source import HerdrSource
from relaydeck.gateway.app import create_app
from relaydeck.journal import digest_for
from tests.security.helpers import auth, mint_token
from tests.support.asgi_socket import AsgiSocket
from tests.support.gateway.asgi_terminal import AsgiTerminal
from tests.support.sync.transport_fixtures import RecordingSource, _state, _wait


class CommandSource(RecordingSource):
    def __init__(self, fault: str = "") -> None:
        super().__init__()
        self.fault = fault
        self.revision = 43
        self.prompts: list[tuple[str, str]] = []

    async def agent_revision(self, target: str) -> int | None:
        return self.revision

    async def agent_prompt(self, target: str, text: str) -> dict[str, Any]:
        self.prompts.append((target, text))
        if self.fault == "unknown":
            raise TimeoutError("synthetic lost upstream result")
        if self.fault:
            raise HerdrError(self.fault, "synthetic upstream refusal")
        return {"type": "agent_prompted"}


@pytest.mark.parametrize(
    "fault,expected",
    [
        ("", SEND_DELIVERED_VIA_PROMPT),
        ("agent_not_ready", SEND_FAILED),
        ("unknown", SEND_UNKNOWN),
        ("agent_blocked", SEND_DELIVERED_VIA_ATTACH),
        ("blocked_write_failed", SEND_DELIVERED_VIA_ATTACH),
        ("revision_changed", SEND_FAILED),
        ("epoch_changed", SEND_FAILED),
    ],
)
def test_factory_observes_one_submitted_command_and_preserves_draft(
    tmp_path: Path,
    fault: str,
    expected: str,
) -> None:
    async def scenario() -> None:
        state, backend, _ = _state(tmp_path)
        now = int(time.time() * 1000)
        state.now_ms = backend.now_ms = lambda: now
        source_fault = "agent_blocked" if fault == "blocked_write_failed" else fault
        source = CommandSource(
            "" if fault in ("revision_changed", "epoch_changed") else source_fault
        )
        if fault == "revision_changed":
            source.revision = 44  # Gateway sees43; fresh dispatch sees a changed upstream revision.
        app = create_app(state)
        connector_token = state.hosts.enroll("home-mac", account="acct-1")
        token = mint_token(state, now=now)
        principal = state.sessions.lookup(token, now)
        assert principal is not None

        async def get_token() -> str:
            return token

        async def opener() -> Any:
            return await AsgiSocket(app, "/v1/connectors", token=connector_token).open()

        link = ConnectorLink(
            LinkConfig(
                "http://127.0.0.1:8125",
                "home-mac",
                connector_token,
                journal_path=str(tmp_path / "connector.db"),
                clock=lambda: now,
            ),
            source,
            opener=opener,
        )
        stop = asyncio.Event()
        link_task = asyncio.create_task(link.run(stop))
        session = None
        session_task = None
        http = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://gw")
        transport = HttpxGatewayTransport("http://gw", get_token, http)
        submitted: list[dict[str, Any]] = []

        async def submit(command: dict[str, Any]) -> dict[str, Any]:
            submitted.append(command)
            record = await transport.submit_command(command)
            if fault == "epoch_changed":
                source._boots += 1  # Rebootstrap races after accepted intent, before execution.
            return record

        factory = SessionFactory(
            ops=HttpxShellGatewayOps("http://gw", get_token, http),
            open_stream=lambda lease, ticket: AsgiTerminal(app, lease, ticket),
            submit_command=submit,
            prompt_text=None,
            renew_lease=None,
            clipboard=None,
            device_id=principal.device_id,
            observe_command=CommandObserver(
                transport.command_status, budget_s=3, interval_s=0.01
            ).observe,
        )
        try:
            await _wait(lambda: state.sockets.host_count("home-mac") == 1)
            session = factory.make_agent(
                host_id="home-mac",
                host_epoch=source.epoch,
                agent_id="term_demo1",
                target="term_demo1",
                initial_status="idle",
                expected_revision=43,
            )
            if fault in ("agent_blocked", "blocked_write_failed"):
                session.open()
                assert await session.confirm_takeover() == "live"
                if fault == "blocked_write_failed":
                    source.created[0].fail_write = True
                session_task = asyncio.create_task(session.run())
            text = "أهلاً one prompt"
            session.on_draft_change(text)
            result = await session.store.send()
            assert result == expected
            assert len(submitted) == 1 and submitted[0]["expected_revision"] == 43
            prompt_count = 0 if fault in ("revision_changed", "epoch_changed") else 1
            assert source.prompts == ([] if not prompt_count else [("term_demo1", text)])
            command_id = submitted[0]["command_id"]
            record = await transport.command_status("home-mac", command_id)
            assert record is not None
            if fault == "blocked_write_failed":
                await _wait(lambda: session.store.submission == "unknown")
                assert session.store.draft == text
                assert session.store.messages[-1].state.kind == "unknown"
            else:
                assert session.store.draft == (
                    text if expected in (SEND_FAILED, SEND_UNKNOWN) else ""
                )
            # Dedupe/reconciliation cannot issue a second prompt, including unknown.
            await transport.submit_command(submitted[0])
            await asyncio.sleep(0.1)
            assert len(source.prompts) == prompt_count
            statuses = [
                e["payload"]["status"]
                for e in backend.event_logs["home-mac"]
                if e["type"] == "command.updated" and e["resource_id"] == command_id
            ]
            assert statuses[:2] == ["accepted", "dispatched"]
            assert statuses[-1] == record["status"]
            assert len(statuses) == len(set(statuses))
            if fault in ("agent_blocked", "blocked_write_failed"):
                await _wait(lambda: bool(source.created and source.created[0].writes))
                assert b"".join(source.created[0].writes) == text.encode()
                if fault == "agent_blocked":
                    assert session.store.input_mode == "tools-keys"
            # Same-host foreign account cannot observe or reuse this identity.
            foreign = mint_token(state, account="other-account", now=now)
            response = await http.get(
                f"/v1/hosts/home-mac/commands/{command_id}", headers=auth(foreign)
            )
            assert response.status_code == 404
            before = backend.journal.status(command_id)
            prompt_count, created_count = len(source.prompts), len(source.created)
            response = await http.post(
                "/v1/hosts/home-mac/commands", headers=auth(foreign), json=submitted[0]
            )
            assert response.status_code == 404
            assert response.json()["code"] == "not_found"
            assert command_id not in response.text and text not in response.text
            assert backend.journal.status(command_id) == before
            assert (len(source.prompts), len(source.created)) == (prompt_count, created_count)
        finally:
            if session is not None:
                await session.dispose()
            if session_task is not None:
                await asyncio.wait_for(session_task, 3)
            stop.set()
            await asyncio.wait_for(link_task, 3)
            await source.close()
            await http.aclose()
            backend.journal.close()
            state.push.close()

    asyncio.run(scenario())


def test_observer_status_query_failure_and_wrong_identity_are_unknown() -> None:
    async def scenario() -> None:
        async def unavailable(host: str, command: str) -> Any:
            raise TransportError("unauthenticated", "session expired", False)

        async def foreign(host: str, command: str) -> Any:
            return {"host_id": "other", "command_id": command, "status": "succeeded"}

        for status in (unavailable, foreign):
            with pytest.raises(CommandOutcomeUnknown):
                await CommandObserver(status).observe("home-mac", "owned-command")

    asyncio.run(scenario())


def test_reconnect_discards_late_command_observation_without_replay() -> None:
    async def scenario() -> None:
        entered, complete = asyncio.Event(), asyncio.Event()
        submitted: list[dict[str, Any]] = []

        async def submit(command: dict[str, Any]) -> dict[str, Any]:
            submitted.append(command)
            return {"command_id": command["command_id"]}

        async def observe(host: str, command: str) -> dict[str, Any]:
            assert host == "home-mac" and command == submitted[0]["command_id"]
            entered.set()
            await complete.wait()
            return {"status": "succeeded"}

        store = ComposerStore(
            target="term-1",
            submit_command=submit,
            prompt_text=None,
            observe_command=observe,
            host_id="home-mac",
            host_epoch="epoch-1",
            agent_id="term-1",
        )
        store.replace_draft("preserve late draft")
        pending = asyncio.create_task(store.send())
        await entered.wait()
        store.reconnect()
        complete.set()
        assert await pending == SEND_IGNORED
        assert store.submission == "unknown" and store.draft == "preserve late draft"
        assert len(submitted) == 1

    asyncio.run(scenario())


def test_expired_accepted_command_is_observed_without_dispatch(tmp_path: Path) -> None:
    async def scenario() -> None:
        state, backend, source = _state(tmp_path)
        current = {"now": int(time.time() * 1000)}
        state.now_ms = backend.now_ms = lambda: current["now"]
        token = mint_token(state, now=current["now"])

        async def get_token() -> str:
            return token

        http = httpx.AsyncClient(
            transport=httpx.ASGITransport(app=create_app(state)), base_url="http://gw"
        )
        transport = HttpxGatewayTransport("http://gw", get_token, http)

        async def submit(command: dict[str, Any]) -> dict[str, Any]:
            result = await transport.submit_command(command)
            current["now"] += 125_000
            return result

        factory = SessionFactory(
            HttpxShellGatewayOps("http://gw", get_token, http),
            lambda lease, ticket: AsgiTerminal(create_app(state), lease, ticket),
            submit,
            None,
            None,
            None,
            "synthetic-device",
            observe_command=CommandObserver(transport.command_status).observe,
        )
        session = factory.make_agent(
            host_id="home-mac", host_epoch=source.epoch, agent_id="term_demo1", target="term_demo1"
        )
        try:
            session.on_draft_change("expire without executing")
            assert await session.store.send() == SEND_FAILED
            assert session.store.draft == "expire without executing"
            command = await transport.command_status(
                "home-mac", session.store.last_command_id or ""
            )
            assert command is not None and command["error_code"] == "expired"
            assert backend.drain_outbox(
                "home-mac"
            )  # Marker still refuses this stale queued intent.
            assert not backend.mark_command_dispatched(command["command_id"], current["now"])
            assert not source.created
        finally:
            await session.dispose()
            await http.aclose()
            backend.journal.close()
            state.push.close()

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "field,value",
    [
        ("generation", True),
        ("generation", "43"),
        ("host_epoch", ""),
        ("principal", ""),
        ("expires_ms", False),
    ],
)
def test_link_rejects_invalid_dispatch_before_ack(tmp_path: Path, field: str, value: Any) -> None:
    source = FixtureAdapter()
    link = ConnectorLink(
        LinkConfig(
            "http://127.0.0.1:8125",
            "home-mac",
            "synthetic-host-token",
            journal_path=str(tmp_path / "connector.db"),
        ),
        source,
    )
    target, payload = {"agent_id": "term-1"}, {"text": "no execution"}
    message = {
        "kind": "command.dispatch",
        "command_id": "typed-command",
        "principal": "owner",
        "host_id": "home-mac",
        "host_epoch": "epoch-fixture-1",
        "gateway_kind": "agent.prompt",
        "target": target,
        "payload": payload,
        "digest": digest_for("home-mac", "agent.prompt", target, payload),
        "generation": 43,
        "expires_ms": 100_000,
        field: value,
    }
    try:
        with pytest.raises(RuntimeError, match="typed command dispatch"):
            link._precheck(message)
        assert source.calls == ()
    finally:
        link._journal.close()


def test_source_revision_uses_fresh_agent_list_and_no_prompt() -> None:
    class FreshAdapter(HerdrAdapter):
        calls = 0

        async def agent_list(self) -> list[dict[str, Any]]:
            self.calls += 1
            return [{"terminal_id": "term-1", "revision": self.calls + 40}]

    async def scenario() -> None:
        adapter = FreshAdapter()
        source = HerdrSource(adapter)
        assert await source.agent_revision("term-1") == 41
        assert await source.agent_revision("term-1") == 42
        assert await source.agent_revision("unknown") is None
        assert adapter.calls == 3
        await source.close()

    asyncio.run(scenario())


def test_blocked_failure_receipt_preserves_new_draft_and_ignores_other_frames() -> None:
    async def scenario() -> None:
        async def unused(command: dict[str, Any]) -> dict[str, Any]:
            raise AssertionError("blocked insertion must not submit")

        store = ComposerStore(
            target="term-1",
            host_id="home-mac",
            host_epoch="epoch-1",
            agent_id="term-1",
            submit_command=unused,
            prompt_text=None,
            initial_status="blocked",
            generation_probe=lambda: 7,
        )

        async def insert(data: bytes) -> None:
            store.note_blocked_frame(7, 2)

        store.bind_writer(generation_probe=lambda: 7, sender=insert)
        store.replace_draft("first insert")
        assert await store.send() == SEND_DELIVERED_VIA_ATTACH
        store.replace_draft("new unsent draft")
        store.note_input_uncertain(7, 3)
        store.note_input_uncertain(8, 2)
        assert store.messages[-1].state.kind == "delivered"
        store.note_input_uncertain(7, 2)
        assert store.messages[-1].state.kind == "unknown"
        assert store.draft == "new unsent draft"
        store.clear_sensitive()
        store.note_input_uncertain(7, 2)
        assert store.draft == "" and store.messages == ()

    asyncio.run(scenario())
