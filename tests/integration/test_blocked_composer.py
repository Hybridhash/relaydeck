"""`agent.prompt` intent record at a live gateway, blocked insertion through the real terminal
writer, and source-backed outcomes from a scripted Herdr stub.

The stub replays the frozen upstream fixture shapes (result/error objects
byte-identical to tests/fixtures/upstream/*.json; only the framing `id` is
rewritten to match the adapter's request id). The live user daemon is never
touched. Connector dispatch of the recorded intent is 's.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

import httpx
import pytest

from relaydeck.client.features.composer.commands import agent_prompt_command
from relaydeck.client.features.composer.domain import (
    INPUT_MODE_COMPOSER,
    INPUT_MODE_TOOLS_KEYS,
    MISSING_WRITER_MESSAGE,
    SEND_BLOCKED_REVIEW,
    SEND_DELIVERED_VIA_ATTACH,
    SEND_DELIVERED_VIA_PROMPT,
    SEND_FAILED,
    SUBMISSION_BLOCKED_INSERTED,
    ComposerStore,
)
from relaydeck.client.features.sync.transport import (
    HttpxGatewayTransport,
    TerminalSocket,
    TransportError,
)
from relaydeck.client.features.terminal.controller import TerminalController
from relaydeck.connector.herdr import HerdrAdapter
from relaydeck.contracts.wire import terminal as terminal_wire
from relaydeck.gateway.app import GatewayState, create_app
from relaydeck.gateway.backend import FakeBackend
from tests.support.live_server import LiveGateway

UPSTREAM = Path(__file__).resolve().parent.parent / "fixtures" / "upstream"
GATEWAY_FIXTURES = Path(__file__).resolve().parent.parent / "fixtures" / "gateway"

FULL_CAPS = frozenset(
    {
        "sessions.read",
        "terminal.view",
        "terminal.control",
        "agents.prompt",
        "agents.start",
        "shells.create",
        "terminal.attach",
    }
)


def _seed() -> tuple[GatewayState, FakeBackend]:
    import time

    backend = FakeBackend(now_ms=lambda: int(time.time() * 1000))
    snapshot: dict[str, Any] = json.loads((GATEWAY_FIXTURES / "snapshot.json").read_text())
    backend.snapshots["home-mac"] = snapshot
    state = GatewayState(
        backend=backend,
        cursor_secret=b"blocked-composer-test-secret-32byt!",
        now_ms=lambda: int(time.time() * 1000),
    )
    return state, backend


def _transport(base_url: str, http: httpx.AsyncClient, token: str) -> HttpxGatewayTransport:
    async def get_token() -> str:
        return token

    return HttpxGatewayTransport(base_url=base_url, token=get_token, client=http)


class _ScriptedSocket(TerminalSocket):
    def __init__(self) -> None:
        self.sent_bytes: list[bytes] = []
        self.sent_text: list[str] = []
        self.connected = False
        self.closed = False

    async def connect(self) -> None:
        self.connected = True

    async def send_bytes(self, data: bytes) -> None:
        self.sent_bytes.append(data)

    async def send_text(self, data: str) -> None:
        self.sent_text.append(data)

    async def receive(self) -> tuple[str, Any]:
        raise TransportError("host_offline", "eof")

    async def close(self) -> None:
        self.closed = True


def _controller(sock: _ScriptedSocket, generation: int = 3) -> TerminalController:
    return TerminalController(
        socket_factory=lambda ticket: sock,
        lease_id="lease-1",
        generation=generation,
        mode="control",
        on_bytes=lambda data: None,
    )


class _FakeReader:
    def __init__(self, lines: list[bytes]) -> None:
        self._lines = list(lines)

    async def readline(self) -> bytes:
        if not self._lines:
            return b""
        return self._lines.pop(0)


class _FakeWriter:
    def __init__(self) -> None:
        self.written = bytearray()
        self.closed = False

    def write(self, data: bytes) -> None:
        self.written += data

    async def drain(self) -> None:
        pass

    def close(self) -> None:
        self.closed = True

    async def wait_closed(self) -> None:
        pass


def _fixture_reply(name: str, rid: str) -> bytes:
    """Frozen fixture shape with the framing id rewritten to the request id."""
    doc = json.loads((UPSTREAM / name).read_text())
    doc["id"] = rid
    return (json.dumps(doc) + "\n").encode()


def _stub_daemon(replies: list[bytes]) -> tuple[HerdrAdapter, _FakeWriter]:
    writer = _FakeWriter()
    reader = _FakeReader(replies)

    async def opener() -> tuple[_FakeReader, _FakeWriter]:
        return reader, writer

    return HerdrAdapter(opener=opener), writer


def _daemon_prompt(adapter: HerdrAdapter) -> Callable[[str, str], Awaitable[dict[str, Any]]]:
    async def run(target: str, text: str) -> dict[str, Any]:
        return await adapter.agent_prompt(target, text)

    return run


def _upstream_requests(writer: _FakeWriter) -> list[dict[str, Any]]:
    return [json.loads(line) for line in bytes(writer.written).decode().splitlines()]


class _Ids:
    def __init__(self) -> None:
        self.n = 0

    def __call__(self) -> str:
        self.n += 1
        return f"cmd-live-{self.n}"


def _store(
    submit: Callable[[dict[str, Any]], Awaitable[dict[str, Any]]],
    prompt: Callable[[str, str], Awaitable[dict[str, Any]]],
    controller: TerminalController | None = None,
) -> ComposerStore:
    from relaydeck.client.features.composer.domain import bind_blocked_writer

    store = ComposerStore(
        target="w1:p1",
        submit_command=submit,
        prompt_text=prompt,
        host_id="home-mac",
        host_epoch="epoch-7",
        agent_id="w1:p1",
        command_id_factory=_Ids(),
        not_ready_delay_s=0.001,
        not_ready_budget_s=0.05,
    )
    if controller is not None:
        bind_blocked_writer(store, controller)
    return store


async def _never_submit(command: dict[str, Any]) -> dict[str, Any]:
    raise AssertionError(f"no gateway submit expected: {command['command_id']}")


async def _never_prompt(target: str, text: str) -> dict[str, Any]:
    raise AssertionError(f"no upstream prompt expected: {target} {text!r}")


# -- gateway intent record (live) -----------------------------------------------------


def test_prompt_intent_accepted_live() -> None:
    async def scenario() -> None:
        state, backend = _seed()
        access, _ = state.sessions.create(
            "acct-1", FULL_CAPS, frozenset({"home-mac"}), state.now_ms()
        )
        with LiveGateway(create_app(state)) as live:
            async with httpx.AsyncClient(base_url=live.base_url, timeout=10.0) as http:
                transport = _transport(live.base_url, http, access)
                body = agent_prompt_command(
                    command_id="cmd-live-001",
                    host_id="home-mac",
                    host_epoch="epoch-7",
                    agent_id="w1:p1",
                    text="Explain the failing test",
                )
                record = await transport.submit_command(body)
                assert record["kind"] == "agent.prompt"
                assert record["status"] == "accepted"
                stored = backend.commands["cmd-live-001"]
                assert stored["payload"] == {"text": "Explain the failing test"}
                assert stored["target"] == {"agent_id": "w1:p1"}
                status = await transport.command_status("home-mac", "cmd-live-001")
                assert status is not None and status["command_id"] == "cmd-live-001"
                assert await transport.command_status("home-mac", "missing") is None

    asyncio.run(scenario())


def test_prompt_intent_dedup_and_conflict_live() -> None:
    async def scenario() -> None:
        state, _ = _seed()
        access, _ = state.sessions.create(
            "acct-1", FULL_CAPS, frozenset({"home-mac"}), state.now_ms()
        )
        with LiveGateway(create_app(state)) as live:
            async with httpx.AsyncClient(base_url=live.base_url, timeout=10.0) as http:
                transport = _transport(live.base_url, http, access)
                first = agent_prompt_command(
                    command_id="cmd-live-002",
                    host_id="home-mac",
                    host_epoch="epoch-7",
                    agent_id="w1:p1",
                    text="hi",
                )
                assert (await transport.submit_command(first))["status"] == "accepted"
                assert (await transport.submit_command(first))["status"] == "accepted"
                clash = dict(first, payload={"text": "other"})
                with pytest.raises(TransportError) as exc:
                    await transport.submit_command(clash)
                assert exc.value.code == "command_conflict"

    asyncio.run(scenario())


def test_prompt_validation_live() -> None:
    async def scenario() -> None:
        state, _ = _seed()
        access, _ = state.sessions.create(
            "acct-1", FULL_CAPS, frozenset({"home-mac"}), state.now_ms()
        )
        thin, _ = state.sessions.create(
            "acct-1", frozenset({"sessions.read"}), frozenset({"home-mac"}), state.now_ms()
        )
        with LiveGateway(create_app(state)) as live:
            async with httpx.AsyncClient(base_url=live.base_url, timeout=10.0) as http:
                transport = _transport(live.base_url, http, access)
                blank = agent_prompt_command(
                    command_id="cmd-live-003",
                    host_id="home-mac",
                    host_epoch="epoch-7",
                    agent_id="w1:p1",
                    text="   ",
                )
                with pytest.raises(TransportError) as exc:
                    await transport.submit_command(blank)
                assert exc.value.code == "forbidden"
                wrong_target = dict(
                    agent_prompt_command(
                        command_id="cmd-live-004",
                        host_id="home-mac",
                        host_epoch="epoch-7",
                        agent_id="w1:p1",
                        text="hi",
                    ),
                    target={"terminal_id": "w1:p1"},
                )
                with pytest.raises(TransportError) as exc:
                    await transport.submit_command(wrong_target)
                assert exc.value.code == "forbidden"
                unprivileged = _transport(live.base_url, http, thin)
                gated = agent_prompt_command(
                    command_id="cmd-live-005",
                    host_id="home-mac",
                    host_epoch="epoch-7",
                    agent_id="w1:p1",
                    text="hi",
                )
                with pytest.raises(TransportError) as exc:
                    await unprivileged.submit_command(gated)
                assert exc.value.code == "unsupported_capability"

    asyncio.run(scenario())


# -- blocked path through the real writer -----------------------------------------------


def test_blocked_insert_through_real_writer_golden() -> None:
    async def scenario() -> None:
        sock = _ScriptedSocket()
        controller = _controller(sock)
        await controller.start("rdt_test")
        store = _store(_never_submit, _never_prompt, controller)
        store.agent_status_did_change("blocked")
        draft = "fix it 😀 now"
        store.replace_draft(draft)
        assert await store.send() == SEND_DELIVERED_VIA_ATTACH
        assert len(sock.sent_bytes) == 1
        chunk = terminal_wire.decode_input(sock.sent_bytes[0])
        assert (chunk.lease_id, chunk.generation, chunk.kind) == ("lease-1", 3, "text")
        assert chunk.input_seq == 1
        assert chunk.data == draft.encode()
        assert chunk.data == b"fix it \xf0\x9f\x98\x80 now"
        assert b"\x1b[200~" not in chunk.data and b"\x1b[201~" not in chunk.data
        assert store.submission == SUBMISSION_BLOCKED_INSERTED
        assert store.input_mode == INPUT_MODE_TOOLS_KEYS

    asyncio.run(scenario())


def test_blocked_multiline_review_through_real_writer() -> None:
    async def scenario() -> None:
        sock = _ScriptedSocket()
        controller = _controller(sock)
        await controller.start("rdt_test")
        store = _store(_never_submit, _never_prompt, controller)
        store.agent_status_did_change("blocked")
        draft = "line one\nline two 😀\n"
        store.replace_draft(draft)
        assert await store.send() == SEND_BLOCKED_REVIEW
        assert sock.sent_bytes == []
        pending = store.pending_blocked
        assert pending is not None
        assert (pending.review.line_count, pending.review.character_count) == (3, len(draft))
        assert pending.review.preview == draft
        assert await store.confirm_blocked_insert() is True
        assert (
            b"".join(terminal_wire.decode_input(frame).data for frame in sock.sent_bytes)
            == draft.encode()
        )
        assert (
            b"".join(terminal_wire.decode_input(frame).data for frame in sock.sent_bytes)
            == "line one\nline two 😀\n".encode()
        )

    asyncio.run(scenario())


def test_blocked_missing_writer_live_fails_explicitly() -> None:
    async def scenario() -> None:
        sock = _ScriptedSocket()
        controller = _controller(sock)
        # Never started: no live writer.
        store = _store(_never_submit, _never_prompt, controller)
        store.agent_status_did_change("blocked")
        store.replace_draft("hi")
        assert await store.send() == SEND_FAILED
        assert store.error == MISSING_WRITER_MESSAGE
        assert sock.sent_bytes == []
        # Started then closed: the writer is dead, not retained.
        await controller.start("rdt_test")
        await controller.close()
        assert controller.paste_generation() is None
        assert await store.send() == SEND_FAILED
        assert store.error == MISSING_WRITER_MESSAGE
        assert sock.sent_bytes == []
        assert store.input_mode == INPUT_MODE_COMPOSER

    asyncio.run(scenario())


# -- scripted Herdr stub outcomes ----------------------------------------------------------


def test_scripted_daemon_ack_full_stack() -> None:
    async def scenario() -> None:
        adapter, writer = _stub_daemon([_fixture_reply("agent_prompted.json", "rd-1")])
        state, backend = _seed()
        access, _ = state.sessions.create(
            "acct-1", FULL_CAPS, frozenset({"home-mac"}), state.now_ms()
        )
        with LiveGateway(create_app(state)) as live:
            async with httpx.AsyncClient(base_url=live.base_url, timeout=10.0) as http:
                transport = _transport(live.base_url, http, access)
                store = _store(transport.submit_command, _daemon_prompt(adapter))
                store.replace_draft("Explain the failing test")
                assert await store.send() == SEND_DELIVERED_VIA_PROMPT
                assert store.draft == ""
                (command_id,) = list(backend.commands)
                assert backend.commands[command_id]["payload"] == {
                    "text": "Explain the failing test"
                }
                (request,) = _upstream_requests(writer)
                assert request["method"] == "agent.prompt"
                assert request["params"] == {
                    "target": "w1:p1",
                    "text": "Explain the failing test",
                }

    asyncio.run(scenario())


def test_scripted_daemon_racing_blocked_inserts_live() -> None:
    async def scenario() -> None:
        adapter, writer = _stub_daemon([_fixture_reply("error_agent_blocked.json", "rd-1")])
        sock = _ScriptedSocket()
        controller = _controller(sock)
        await controller.start("rdt_test")
        state, backend = _seed()
        access, _ = state.sessions.create(
            "acct-1", FULL_CAPS, frozenset({"home-mac"}), state.now_ms()
        )
        with LiveGateway(create_app(state)) as live:
            async with httpx.AsyncClient(base_url=live.base_url, timeout=10.0) as http:
                transport = _transport(live.base_url, http, access)
                store = _store(transport.submit_command, _daemon_prompt(adapter), controller)
                store.replace_draft("race 😀")
                assert await store.send() == SEND_DELIVERED_VIA_ATTACH
                assert len(backend.commands) == 1  # one intent, no resubmit
                assert len(_upstream_requests(writer)) == 1  # one prompt, no retry
                assert len(sock.sent_bytes) == 1
                chunk = terminal_wire.decode_input(sock.sent_bytes[0])
                assert chunk.kind == "text"
                assert chunk.data == "race 😀".encode()
                assert store.input_mode == INPUT_MODE_TOOLS_KEYS

    asyncio.run(scenario())


def test_scripted_daemon_not_ready_then_ack() -> None:
    async def scenario() -> None:
        adapter, writer = _stub_daemon(
            [
                _fixture_reply("error_agent_not_ready.json", "rd-1"),
                _fixture_reply("agent_prompted.json", "rd-2"),
            ]
        )
        state, backend = _seed()
        access, _ = state.sessions.create(
            "acct-1", FULL_CAPS, frozenset({"home-mac"}), state.now_ms()
        )
        with LiveGateway(create_app(state)) as live:
            async with httpx.AsyncClient(base_url=live.base_url, timeout=10.0) as http:
                transport = _transport(live.base_url, http, access)
                store = _store(transport.submit_command, _daemon_prompt(adapter))
                store.replace_draft("hi")
                assert await store.send() == SEND_DELIVERED_VIA_PROMPT
                assert len(backend.commands) == 1  # retries re-await; never resubmit
                requests = _upstream_requests(writer)
                assert len(requests) == 2
                assert all(r["method"] == "agent.prompt" for r in requests)
                assert all(r["params"] == {"target": "w1:p1", "text": "hi"} for r in requests)

    asyncio.run(scenario())


# -- reconnect and approval-inertness ----------------------------------------------------------


def test_reconnect_no_replay_live_writer() -> None:
    async def scenario() -> None:
        sock = _ScriptedSocket()
        controller = _controller(sock)
        await controller.start("rdt_test")
        state, backend = _seed()
        access, _ = state.sessions.create(
            "acct-1", FULL_CAPS, frozenset({"home-mac"}), state.now_ms()
        )
        with LiveGateway(create_app(state)) as live:
            async with httpx.AsyncClient(base_url=live.base_url, timeout=10.0) as http:
                transport = _transport(live.base_url, http, access)
                store = _store(transport.submit_command, _never_prompt, controller)
                store.agent_status_did_change("blocked")
                store.replace_draft("a\nb")
                assert await store.send() == SEND_BLOCKED_REVIEW
                store.reconnect()
                await controller.close()
                store.unbind_writer()
                assert store.draft == "a\nb"
                assert sock.sent_bytes == []
                assert backend.commands == {}
                assert await store.confirm_blocked_insert() is False
                assert sock.sent_bytes == []

    asyncio.run(scenario())


def test_approval_labels_inert_live() -> None:
    async def scenario() -> None:
        sock = _ScriptedSocket()
        controller = _controller(sock)
        await controller.start("rdt_test")
        state, backend = _seed()
        access, _ = state.sessions.create(
            "acct-1", FULL_CAPS, frozenset({"home-mac"}), state.now_ms()
        )
        with LiveGateway(create_app(state)) as live:
            async with httpx.AsyncClient(base_url=live.base_url, timeout=10.0) as http:
                transport = _transport(live.base_url, http, access)
                store = _store(transport.submit_command, _never_prompt, controller)
                store.replace_draft("my draft")
                labels = (
                    "Approve? [y/N]",
                    "Do you want to proceed? (Y/n)",
                    "Agent needs approval: delete 3 files? [Approve] [Deny]",
                )
                for label in labels:
                    store.agent_status_did_change("blocked", label)
                    store.agent_status_did_change("working", label)
                assert backend.commands == {}
                assert sock.sent_bytes == []
                assert store.messages == ()
                store.agent_status_did_change("blocked", "Approve? [y/N]")
                assert await store.send() == SEND_DELIVERED_VIA_ATTACH
                assert len(sock.sent_bytes) == 1
                assert terminal_wire.decode_input(sock.sent_bytes[0]).data == b"my draft"

    asyncio.run(scenario())
