"""Shared tracking: uncertainty survives replacement; recovery never resends."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import flet as ft
import httpx
import pytest

from relaydeck.client.adapters.server_commands import ServerCommandStore
from relaydeck.client.features.auth.service import REFRESH_KEY
from relaydeck.client.features.composer.observation import CommandObserver
from relaydeck.client.features.create.commands import agent_start_command
from relaydeck.client.features.sync.transport import HttpxGatewayTransport, TransportError
from relaydeck.client.packaged_runtime import PackagedRuntime
from relaydeck.client.pending_commands import MAX_BYTES, MAX_RECORDS, PendingCommands, RecoveryError
from relaydeck.gateway.app import create_app
from tests.security.helpers import ALL_CAPS, NOW, make_state
from tests.support.creation.pending_command_records import command, record
from tests.support.fakes import FakeClipboard, FakeCredentialStore
from tests.support.host_harness import render_component, run
from tests.support.sync.lifecycle import _agent
from tests.support.ui.packaged import Preferences


async def token() -> str:
    return "SYNTHETIC_BEARER"


def test_concurrent_shared_store_same_ids_on_two_hosts_and_lost_response() -> None:
    async def scenario() -> None:
        store = FakeCredentialStore()
        requests: list[tuple[str, str]] = []
        accepted: dict[tuple[str, str], dict[str, Any]] = {}

        def handler(request: httpx.Request) -> httpx.Response:
            requests.append((request.method, request.url.path))
            if request.method == "POST":
                body = json.loads(request.content)
                accepted[(body["host_id"], body["command_id"])] = record(body)
                raise httpx.ReadError("response lost after acceptance")
            host, identity = request.url.path.split("/")[3::2]
            return httpx.Response(200, json=accepted[(host, identity)])

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
            a = HttpxGatewayTransport("https://gw", token, http)
            b = HttpxGatewayTransport("https://gw", token, http)
            a.commands = PendingCommands(store, "https://gw", "acct-1", "native-1")
            b.commands = PendingCommands(store, "https://gw", "acct-1", "native-1")
            outcomes = await asyncio.gather(
                a.submit_command(command("same", "home-mac")),
                b.submit_command(command("same", "other-mac")),
                return_exceptions=True,
            )
            assert all(isinstance(outcome, TransportError) for outcome in outcomes)
            replacement = PendingCommands(store, "https://gw", "acct-1", "native-1")
            await replacement.load()
            assert set(replacement.records) == {
                ("home-mac", "pending-same"),
                ("other-mac", "pending-same"),
            }
            encoded = await store.get(replacement.key)
            assert encoded and "PRIVATE_PROMPT" not in encoded and "SYNTHETIC_BEARER" not in encoded
            a.commands = replacement
            posts = len([r for r in requests if r[0] == "POST"])
            await replacement.reconcile(a.command_status, frozenset({"home-mac", "other-mac"}))
            assert {r["outcome"] for r in replacement.records.values()} == {"accepted"}
            assert len([r for r in requests if r[0] == "POST"]) == posts == 2
            with pytest.raises(RecoveryError):
                await a.submit_command(command("new-id", "home-mac"))
            assert len([r for r in requests if r[0] == "POST"]) == 2

    run(scenario())


def test_storage_failure_capacity_corruption_and_scope_boundaries() -> None:
    async def scenario() -> None:
        class FailedStore(FakeCredentialStore):
            async def set(self, key: str, value: str) -> None:
                raise OSError("PRIVATE_FAILURE")

        calls = []

        def unexpected(request: httpx.Request) -> httpx.Response:
            calls.append(request)
            raise AssertionError("Unexpected command HTTP request")

        async with httpx.AsyncClient(transport=httpx.MockTransport(unexpected)) as http:
            transport = HttpxGatewayTransport("https://gw", token, http)
            transport.commands = PendingCommands(FailedStore(), "https://gw", "acct-1", "d1")
            with pytest.raises(RecoveryError):
                await transport.submit_command(command())
            assert not calls
            store = FakeCredentialStore()
            journal = PendingCommands(store, "https://gw", "acct-1", "d1")
            for i in range(MAX_RECORDS):
                await journal.begin(command(str(i), target=f"agent-{i}"))
            with pytest.raises(RecoveryError, match="capacity"):
                await journal.begin(command("over", target="new-agent"))
            assert len(journal.records) == MAX_RECORDS
            for origin, account, device in [
                ("https://new", "acct-1", "d1"),
                ("https://gw", "acct-2", "d1"),
                ("https://gw", "acct-1", "d2"),
            ]:
                foreign = PendingCommands(store, origin, account, device)
                await foreign.reconcile(transport.command_status, frozenset({"home-mac"}))
                assert foreign.records == {} and not calls
            for bad in [
                "{",
                "x" * (MAX_BYTES + 1),
                '{"version":1,"version":1}',
                json.dumps({"version": True, "scope": journal.scope, "records": []}),
                json.dumps(
                    {"version": 1, "scope": journal.scope, "records": [{"payload": "private"}]}
                ),
            ]:
                await store.set(journal.key, bad)
                transport.commands = PendingCommands(store, "https://gw", "acct-1", "d1")
                with pytest.raises(RecoveryError):
                    await transport.submit_command(command("corrupt"))
                assert not calls

    run(scenario())


def test_partial_creation_requires_review_unknown_cannot_be_dismissed() -> None:
    async def scenario() -> None:
        journal = PendingCommands(FakeCredentialStore(), "https://gw", "acct-1", "d1")
        body = agent_start_command(
            command_id="create-id",
            host_id="home-mac",
            host_epoch="epoch-7",
            workspace_id="",
            kind="agent-a",
            new_workspace={"directory": "/PRIVATE/PATH", "label": "PRIVATE_TITLE"},
        )
        await journal.begin(body)
        partial = record(
            body,
            "failed",
            {"type": "partial_creation", "workspace_id": "w2", "tab_id": "t2", "pane_id": "p2"},
        )
        partial["error_code"] = "agent_not_ready"
        await journal.observe("home-mac", "create-id", partial)
        assert journal.records[("home-mac", "create-id")]["created"] == {
            "workspace_id": "w2",
            "tab_id": "t2",
            "pane_id": "p2",
        }
        with pytest.raises(RecoveryError):
            await journal.begin({**body, "command_id": "duplicate-workspace"})
        await journal.acknowledge("home-mac", "create-id")
        await journal.begin({**body, "command_id": "reviewed-new-action"})
        await journal.observe("home-mac", "reviewed-new-action", None)
        with pytest.raises(RecoveryError):
            await journal.acknowledge("home-mac", "reviewed-new-action")
        expired = record({**body, "command_id": "reviewed-new-action"}, "expired")
        await journal.observe("home-mac", "reviewed-new-action", expired)
        assert journal.records[("home-mac", "reviewed-new-action")]["outcome"] == "unknown"
        assert "PRIVATE" not in str(await journal.store.get(journal.key))

    run(scenario())


def test_composer_and_cancelled_observer_use_shared_durable_boundary() -> None:
    async def scenario() -> None:
        store = FakeCredentialStore()
        posts = []

        async def handler(request: httpx.Request) -> httpx.Response:
            if request.method == "POST":
                body = json.loads(request.content)
                posts.append(body)
                return httpx.Response(202, json=record(body))
            await asyncio.Future()
            raise AssertionError

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
            transport = HttpxGatewayTransport("https://gw", token, http)
            transport.commands = PendingCommands(store, "https://gw", "acct-1", "d1")
            session, _, _, _ = _agent()
            session.store._submit_command = transport.submit_command
            session.store._observe_command = CommandObserver(transport.command_status).observe
            session.open()
            await session.confirm_takeover()
            session.on_draft_change("PRIVATE_COMPOSER")
            task = asyncio.create_task(session.on_send())
            for _ in range(100):
                if posts:
                    break
                await asyncio.sleep(0)
            assert len(posts) == 1
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
            replacement = PendingCommands(store, "https://gw", "acct-1", "d1")
            await replacement.load()
            assert len(replacement.records) == 1
            assert "PRIVATE_COMPOSER" not in str(await store.get(replacement.key))
            assert session.store.draft == "PRIVATE_COMPOSER"
            await session.dispose()

    run(scenario())


@pytest.mark.parametrize("cleanup", ["logout", "origin"])
def test_packaged_process_replacement_queries_all_ids_without_retained_terminals(
    cleanup: str,
) -> None:
    async def scenario() -> None:
        state, _ = make_state()
        _, refresh = state.sessions.create("acct-1", ALL_CAPS, {"home-mac"}, NOW, "native-login")
        secure = FakeCredentialStore()
        await secure.set(REFRESH_KEY, refresh)
        await secure.set("identity_origin", "https://gw")
        journal = PendingCommands(secure, "https://gw", "acct-1", "native-login")
        await journal.begin(command("one", target="agent-1"))
        await journal.begin(command("two", target="agent-2"))
        prefs = Preferences({"relaydeck-endpoint": "https://gw"})
        app = await render_component(lambda: ft.Text("Native double"))
        requests = []

        async def requested(request: httpx.Request) -> None:
            requests.append((request.method, request.url.path))

        async def browser(url: str) -> None:
            pytest.fail("Recovery cannot authorize a new login")

        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=create_app(state)),
            event_hooks={"request": [requested]},
        ) as http:
            for _ in range(2):
                runtime = PackagedRuntime(
                    app.page,
                    secure,
                    prefs,
                    browser,
                    lambda: None,
                    client=http,
                    clipboard=FakeClipboard(),
                )
                await runtime.start()
                assert runtime.resources is not None and not runtime.resources.retained.entries()
                assert len(runtime.resources.commands.records) == 2
                assert {r["outcome"] for r in runtime.resources.commands.records.values()} == {
                    "unknown"
                }
                await runtime.close()
            assert len([r for r in requests if r[0] == "GET" and "/commands/" in r[1]]) == 4
            assert not any(r[0] == "POST" and r[1].endswith("/commands") for r in requests)
            runtime = PackagedRuntime(app.page, secure, prefs, browser, lambda: None, client=http)
            await runtime.start()
            if cleanup == "origin":
                await runtime.configure("https://new.private")
            else:
                await runtime.logout()
            assert await secure.get(journal.key) is None
            await runtime.close()
        state.push.close()

    run(scenario())


def test_lifecycle_and_identity_fence_after_persistence_before_token_return() -> None:
    async def scenario() -> None:
        store = FakeCredentialStore()
        version = [1]
        waiting = asyncio.Event()
        release = asyncio.Event()
        requests = []

        async def delayed_token() -> str:
            waiting.set()
            await release.wait()
            return "different-identity-token"

        def unexpected(request: httpx.Request) -> httpx.Response:
            requests.append(request)
            raise AssertionError("Unexpected command HTTP request")

        async with httpx.AsyncClient(transport=httpx.MockTransport(unexpected)) as http:
            transport = HttpxGatewayTransport("https://gw", delayed_token, http)
            journal = PendingCommands(
                store, "https://gw", "acct-1", "d1", version=lambda: version[0]
            )
            transport.commands = journal
            task = asyncio.create_task(transport.submit_command(command()))
            await waiting.wait()
            assert len(journal.records) == 1
            version[0] += 1
            release.set()
            with pytest.raises(TransportError, match="lifecycle"):
                await task
            assert not requests
            await journal.close()
            with pytest.raises(RecoveryError):
                await transport.command_status("home-mac", "pending-one")
            assert not requests

    run(scenario())


def test_server_owned_store_survives_replacement_and_rejects_non_private_files(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        path = tmp_path / "pending.db"
        store = ServerCommandStore(path)
        journal = PendingCommands(store, "https://mac.private", "acct-1", "d1")
        await journal.begin(command())
        store.close()
        restored = ServerCommandStore(path)
        replacement = PendingCommands(restored, "https://mac.private", "acct-1", "d1")
        await replacement.load()
        assert len(replacement.records) == 1
        restored.close()
        path.chmod(0o644)
        with pytest.raises(ValueError):
            ServerCommandStore(path)
        link = tmp_path / "linked.db"
        link.symlink_to(path)
        with pytest.raises(OSError):
            ServerCommandStore(link)

    run(scenario())
