"""View-only bounded recovery and closed-reader races."""

from __future__ import annotations

import asyncio
import json
from typing import Any

import pytest
from relaydeck_terminal.terminal import RelayDeckTerminal

from relaydeck.client.features.shell.ops import ShellOpError
from relaydeck.client.features.sync.controller import TerminalStreamClient
from relaydeck.client.features.sync.transport import TransportError
from relaydeck.client.features.terminal import agent_session
from relaydeck.client.features.terminal.stream import EMULATOR_PRESERVED
from relaydeck.client.features.terminal.warm_watch import watch_only
from relaydeck.client.retained_sessions import RetainedSessions
from relaydeck.gateway import terminal_wire
from tests.support.scripted_sessions import ScriptedTerminalSocket, make_scripted_sessions
from tests.support.terminal.watch_drops import until, watching


def test_double_close_captures_socket_before_await_and_preserves_new_reader() -> None:
    async def scenario() -> None:
        entered, finish = asyncio.Event(), asyncio.Event()

        class SlowBye(ScriptedTerminalSocket):
            closes = 0

            async def send_text(self, data: str) -> None:
                if json.loads(data)["kind"] == "bye":
                    entered.set()
                    await finish.wait()
                await super().send_text(data)

            async def close(self) -> None:
                self.closes += 1
                await super().close()

        old, new = SlowBye(), ScriptedTerminalSocket()
        client = TerminalStreamClient(
            lambda ticket: old if ticket == "old" else new,
            lambda *_: None,
            lambda _: None,
            lambda _: None,
        )
        await client.open("lease", 1, "old")
        first = asyncio.create_task(client.close())
        await entered.wait()
        assert client._socket is None
        await client.close()
        assert not await client.resize(80, 24)
        assert client._input_seq == 0
        with pytest.raises(TransportError, match="Terminal stream closed"):
            await client.send_input(b"never replay")
        await client.open("lease", 1, "new")
        finish.set()
        await first
        assert old.closes == 1 and old.closed
        assert client._socket is new and not new.closed
        assert await client.resize(80, 24)
        await client.close()

    asyncio.run(scenario())


def test_run_ignores_a_late_frame_from_a_closed_replaced_socket() -> None:
    async def scenario() -> None:
        entered, finish = asyncio.Event(), asyncio.Event()
        applied: list[bytes] = []

        class LateFrame(ScriptedTerminalSocket):
            async def receive(self) -> tuple[str, Any]:
                entered.set()
                await finish.wait()
                return "bytes", terminal_wire.encode_output("e1", 0, b"late")

        old, new = LateFrame(), ScriptedTerminalSocket()
        client = TerminalStreamClient(
            lambda ticket: old if ticket == "old" else new,
            lambda _, data: applied.append(data),
            lambda _: None,
            lambda _: None,
        )
        await client.open("lease", 1, "old")
        reader = asyncio.create_task(client.run())
        await entered.wait()
        await client.close()
        await client.open("lease", 1, "new")
        finish.set()
        await reader
        assert not applied and client.next_offset == 0
        await client.close()

    asyncio.run(scenario())


@pytest.mark.parametrize("replaced", [False, True])
@pytest.mark.parametrize(
    "failure", [TransportError("host_offline", "old reader"), OSError("old reader")]
)
def test_receive_failure_from_retired_socket_is_ignored_but_current_failure_propagates(
    replaced: bool,
    failure: Exception,
) -> None:
    async def scenario() -> None:
        entered, finish = asyncio.Event(), asyncio.Event()

        class LateError(ScriptedTerminalSocket):
            async def receive(self) -> tuple[str, Any]:
                entered.set()
                await finish.wait()
                raise failure

        old, new = LateError(), ScriptedTerminalSocket()
        output, gaps = [], []
        client = TerminalStreamClient(
            lambda ticket: old if ticket == "old" else new,
            lambda _, data: output.append(data),
            lambda _: None,
            gaps.append,
        )
        await client.open("lease", 1, "old")
        reader = asyncio.create_task(client.run())
        await entered.wait()
        if replaced:
            await client.close()
            await client.open("lease", 1, "new")
        finish.set()
        if replaced:
            await reader
            assert client._socket is new and not new.closed
        else:
            with pytest.raises(type(failure)) as caught:
                await reader
            assert caught.value is failure
        assert not output and not gaps and client.next_offset == 0
        await client.close()

    asyncio.run(scenario())


@pytest.mark.parametrize("reconnect_first", [False, True])
def test_explicit_control_survives_old_watch_renewal_shutdown(
    monkeypatch: pytest.MonkeyPatch,
    reconnect_first: bool,
) -> None:
    monkeypatch.setattr(agent_session, "WATCH_RETRY_DELAYS_S", (0, 0, 0, 0, 0))

    async def scenario() -> None:
        scripted, session, term = await watching()
        entered, finish = asyncio.Event(), asyncio.Event()

        class DelayedRenewal:
            async def stop(self) -> None:
                entered.set()
                try:
                    await finish.wait()
                except asyncio.CancelledError:
                    # Cancellation cleanup can finish late. Exercise the fence
                    # independently of whether the underlying await propagates.
                    asyncio.current_task().uncancel()
                    await finish.wait()

            def cancel(self) -> None:
                pass

        session._renewal = DelayedRenewal()
        reader = asyncio.create_task(session.run())
        try:
            await scripted.streams.opened[-1][2].close()
            await entered.wait()
            recovery = session._watch_retry_task
            assert recovery is not None and not recovery.done()
            if reconnect_first:
                await session.reconnect()
            else:
                session.request_takeover()
            await session.confirm_takeover()
            control, lease, token = session.controller, session.lease, session.input_token
            assert lease.mode == "control"
            baseline = (
                list(scripted.ops.leases),
                list(scripted.ops.releases),
                len(scripted.ops.attaches),
                len(scripted.streams.opened),
            )
            finish.set()
            await until(lambda: reader.done() or session.lease is not lease)
            assert session.controller is control and session.lease is lease
            assert session.phase == "live" and session.input_token == token
            assert lease.lease_id not in scripted.ops.releases
            assert (
                scripted.ops.leases,
                scripted.ops.releases,
                len(scripted.ops.attaches),
                len(scripted.streams.opened),
            ) == baseline
            assert session.current_term is term and session.store.draft == "unsent draft"
            assert not scripted.submitted and not scripted.prompted
            assert all(
                kind != "bytes" for _, _, sock in scripted.streams.opened for kind, _ in sock.sent
            )
            await reader
            assert session._watch_retry_task is None
        finally:
            finish.set()
            await session.dispose()
            await asyncio.wait_for(reader, 2)

    asyncio.run(scenario())


def test_takeover_intent_invalidates_watch_before_retry_task_exists(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(agent_session, "WATCH_RETRY_DELAYS_S", (0, 0, 0, 0, 0))

    async def scenario() -> None:
        scripted, session, _ = await watching()
        entered = asyncio.Event()
        intent = False

        def takeover(live: Any) -> None:
            nonlocal intent
            if live.phase == "disconnected" and not intent:
                intent = True
                assert live._watch_retry_task is None
                entered.set()
                live.request_takeover()

        session.subscribe(takeover)
        leases = list(scripted.ops.leases)
        releases = list(scripted.ops.releases)
        reader = asyncio.create_task(session.run())
        try:
            await scripted.streams.opened[-1][2].close()
            await entered.wait()
            # Old code would enter a new watch stream indefinitely; release
            # when it either completes or has acquired an unauthorized view.
            await until(lambda: reader.done() or scripted.ops.leases != leases)
            assert scripted.ops.leases == leases and scripted.ops.releases == releases
            assert session.takeover_disclosure is not None
            await reader
        finally:
            await session.dispose()
            await asyncio.wait_for(reader, 2)

    asyncio.run(scenario())


@pytest.mark.parametrize("failed_tickets", [0, 2])
def test_dropped_watch_resumes_retained_cursor_without_attach_input_or_resize(
    monkeypatch: pytest.MonkeyPatch, failed_tickets: int
) -> None:
    monkeypatch.setattr(agent_session, "WATCH_RETRY_DELAYS_S", (0, 0, 0, 0, 0))

    async def scenario() -> None:
        scripted, session, term = await watching()
        before = len(scripted.ops.leases)
        first = scripted.streams.opened[-1][2]
        reader = asyncio.create_task(session.run())
        first.queue(("bytes", terminal_wire.encode_output(session.lease.epoch, 0, b"abc")))
        await until(lambda: session.term_state.applied_offset == 3)
        old_controller = session.controller
        original_ticket = scripted.ops.terminal_ticket
        remaining = failed_tickets

        async def ticket(lease_id: str) -> str:
            nonlocal remaining
            if remaining:
                remaining -= 1
                raise ShellOpError("host_offline", "synthetic offline")
            return await original_ticket(lease_id)

        monkeypatch.setattr(scripted.ops, "terminal_ticket", ticket)
        notices: list[str] = []
        session.subscribe(lambda live: notices.append(live.notice))
        try:
            await first.close()
            await until(
                lambda: session.controller is not old_controller and session.phase == "live"
            )
            assert session.controller.emulator == EMULATOR_PRESERVED
            assert session.controller._client.resume_query().endswith("offset=3")
            assert session.current_term is term and session.store.draft == "unsent draft"
            assert session.lease.mode == "view" and session.takeover_disclosure is None
            assert agent_session.WATCH_RECONNECT_MESSAGE in notices
            assert len(scripted.ops.leases) == before + failed_tickets + 1
            assert all(item["mode"] == "view" for item in scripted.ops.leases[before:])
            assert len(scripted.ops.attaches) == 1
            new = scripted.streams.opened[-1][2]
            new.queue(("bytes", terminal_wire.encode_output(session.lease.epoch, 3, b"def")))
            await until(lambda: session.term_state.applied_offset == 6)
            assert bytes(session.output_log) == b"abcdef"
            assert not await session.resize(40, 20)
            assert all(
                kind != "bytes" for _, _, sock in scripted.streams.opened for kind, _ in sock.sent
            )
        finally:
            await session.dispose()
            await asyncio.wait_for(reader, 2)

    asyncio.run(scenario())


def test_control_drop_stays_explicit_and_closed_controller_resize_is_quiet() -> None:
    async def scenario() -> None:
        scripted = make_scripted_sessions()
        session = scripted.factory.make_agent(
            host_id="mac", host_epoch="h1", agent_id="a", target="a"
        )
        session.open()
        await session.confirm_takeover()
        controller = session.controller
        reader = asyncio.create_task(session.run())
        await scripted.streams.opened[-1][2].close()
        assert await reader == "disconnected"
        await controller.close()
        assert not await session.resize(40, 20)
        assert len(scripted.ops.leases) == len(scripted.ops.attaches) == 1
        assert session.takeover_disclosure is None
        await session.reconnect()
        assert session.takeover_disclosure is not None
        assert len(scripted.ops.attaches) == 1
        await session.dispose()

    asyncio.run(scenario())


def test_retained_owner_keeps_one_reader_task_and_engine_across_watch_recovery(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(agent_session, "WATCH_RETRY_DELAYS_S", (0, 0, 0, 0, 0))

    async def scenario() -> None:
        scripted = make_scripted_sessions()
        pool = RetainedSessions()
        entry = await pool.acquire(
            "mac",
            "agent",
            "a",
            "h1",
            lambda: scripted.factory.make_agent(
                host_id="mac", host_epoch="h1", agent_id="a", target="a"
            ),
        )
        session = entry.session
        try:
            session.open()
            await session.confirm_takeover()
            term = RelayDeckTerminal()
            session.bind_view(term, write=lambda *_: True)
            session.on_draft_change("retained draft")
            assert await watch_only(session, lambda: True)
            await until(lambda: entry.task is not None and not entry.task.done())
            first = scripted.streams.opened[-1][2]
            first.queue(("bytes", terminal_wire.encode_output(session.lease.epoch, 0, b"abc")))
            await until(lambda: session.term_state.applied_offset == 3)
            reader = entry.task
            controller = session.controller
            await first.close()
            await until(lambda: session.controller is not controller and session.phase == "live")
            assert entry.task is reader and not reader.done()
            assert pool.foreground is entry and session.current_term is term
            assert session.store.draft == "retained draft"
            assert session.controller._client.resume_offset() == 3
            assert session.lease.mode == "view" and len(scripted.ops.attaches) == 1
            assert all(
                kind != "bytes" for _, _, sock in scripted.streams.opened for kind, _ in sock.sent
            )
        finally:
            await pool.shutdown()

    asyncio.run(scenario())


@pytest.mark.parametrize("reject", ["acquire", "immediate_drop"])
def test_watch_retry_budget_is_five_even_when_new_sockets_close_immediately(
    monkeypatch: pytest.MonkeyPatch, reject: str
) -> None:
    assert agent_session.WATCH_RETRY_DELAYS_S == (1, 2, 4, 8, 16)
    monkeypatch.setattr(agent_session, "WATCH_RETRY_DELAYS_S", (0, 0, 0, 0, 0))

    async def scenario() -> None:
        scripted, session, _ = await watching()
        acquire = scripted.ops.acquire_lease
        calls: list[str] = []

        async def failing(attachment: str, mode: str) -> Any:
            calls.append(mode)
            if reject == "acquire":
                raise ShellOpError("host_offline", "synthetic offline")
            return await acquire(attachment, mode)

        monkeypatch.setattr(scripted.ops, "acquire_lease", failing)
        if reject == "immediate_drop":
            original_factory = session._socket_factory

            def disconnected(ticket: str) -> ScriptedTerminalSocket:
                socket = original_factory(ticket)
                socket.closed = True
                return socket

            session._socket_factory = disconnected
        reader = asyncio.create_task(session.run())
        await scripted.streams.opened[-1][2].close()
        assert await asyncio.wait_for(reader, 2) == "disconnected"
        assert calls == ["view"] * 5
        assert session.notice == agent_session.AGENT_DISCONNECT_MESSAGE
        assert session._watch_retry_task is None and len(scripted.ops.attaches) == 1
        await session.dispose()

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "action",
    [
        "navigate",
        "disconnect",
        "pause",
        "dispose",
        "revoke",
        "viewer_reset",
        "unmount",
        "reconnect",
    ],
)
def test_watch_backoff_cancels_on_lifecycle_change(action: str) -> None:
    async def scenario() -> None:
        scripted, session, _ = await watching()
        leases = len(scripted.ops.leases)
        reader = asyncio.create_task(session.run())
        await scripted.streams.opened[-1][2].close()
        await until(lambda: session.notice == agent_session.WATCH_RECONNECT_MESSAGE)
        retry = session._watch_retry_task
        if action == "navigate":
            session.set_foreground(False)
        elif action == "revoke":
            session._on_renewal_terminal("forbidden")
        elif action == "viewer_reset":
            session._viewer_reopened()
        elif action == "unmount":
            session.unbind_view()
        else:
            await getattr(session, action)()
        await asyncio.wait_for(reader, 0.5)
        assert retry.cancelled() and session._watch_retry_task is None
        assert len(scripted.ops.leases) == leases
        assert session.notice != agent_session.WATCH_RECONNECT_MESSAGE
        await session.dispose()

    asyncio.run(scenario())


def test_cancelled_watch_acquisition_releases_late_grant_without_open_or_replay(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(agent_session, "WATCH_RETRY_DELAYS_S", (0, 0, 0, 0, 0))

    async def scenario() -> None:
        scripted, session, _ = await watching()
        entered, cancelled, finish = asyncio.Event(), asyncio.Event(), asyncio.Event()
        acquire = scripted.ops.acquire_lease
        issued: list[Any] = []

        async def delayed(attachment: str, mode: str) -> Any:
            entered.set()
            try:
                await finish.wait()
            except asyncio.CancelledError:
                cancelled.set()
                await finish.wait()
            lease = await acquire(attachment, mode)
            issued.append(lease)
            return lease

        monkeypatch.setattr(scripted.ops, "acquire_lease", delayed)
        opened = len(scripted.streams.opened)
        reader = asyncio.create_task(session.run())
        await scripted.streams.opened[-1][2].close()
        await entered.wait()
        session.set_foreground(False)
        await cancelled.wait()
        finish.set()
        await asyncio.wait_for(reader, 2)
        assert issued[0].lease_id in scripted.ops.releases
        assert len(scripted.streams.opened) == opened and session.lease is None
        assert session._watch_retry_task is None
        await session.dispose()

    asyncio.run(scenario())


@pytest.mark.parametrize("detail", ["closed", "exited", "revoked"])
def test_explicit_terminal_bye_never_auto_reacquires_view(detail: str) -> None:
    async def scenario() -> None:
        scripted, session, _ = await watching()
        leases = len(scripted.ops.leases)
        scripted.streams.opened[-1][2].queue(
            ("text", json.dumps({"kind": "bye", "detail": detail}))
        )
        await session.run()
        assert session.phase == ("exited" if detail in ("closed", "exited") else "disconnected")
        assert len(scripted.ops.leases) == leases
        await session.dispose()

    asyncio.run(scenario())
