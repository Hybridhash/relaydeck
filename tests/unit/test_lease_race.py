"""a retired renewal owns no replacement writer."""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from relaydeck.client.features.sync.transport import TransportError
from tests.support.scripted_sessions import ScriptedTerminalSocket, make_scripted_sessions


async def until(predicate: Any) -> None:
    async with asyncio.timeout(2):
        while not predicate():
            await asyncio.sleep(0)


async def opened(kind: str, renew: Any, open_stream: Any = None) -> tuple[Any, Any]:
    scripted = make_scripted_sessions()
    scripted.factory.renew_lease = renew
    if open_stream is not None:
        scripted.factory.open_stream = open_stream
    if kind == "agent":
        session = scripted.factory.make_agent(
            host_id="mac", host_epoch="h1", agent_id="a", target="a"
        )
        session.open()
        await session.confirm_takeover()
        session.on_draft_change("exact unsent\ndraft")
    else:
        session = scripted.factory.make_shell(host_id="mac")
        await session.open("shell")
    return scripted, session


def replacement(session: Any, operation: str) -> Any:
    if operation == "agent":
        session.request_takeover()
        return session.confirm_takeover()
    if operation == "shell-reconnect":
        return session.reconnect()
    return session.take_over()


@pytest.mark.parametrize("operation", ["agent", "shell-takeover", "shell-reconnect"])
@pytest.mark.parametrize("late_code", ["lease_expired", "unauthenticated"])
def test_retire_driver_before_replacement_and_ignore_late_failure(
    operation: str, late_code: str, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level("INFO", logger="relaydeck.lease_lifecycle")

    async def scenario() -> None:
        entered, cancelled, retired, connecting, connected = (asyncio.Event() for _ in range(5))
        first_id: list[str] = []

        async def renew(lease_id: str) -> dict[str, Any]:
            if lease_id == first_id[0]:
                entered.set()
                try:
                    await asyncio.Event().wait()
                except asyncio.CancelledError:
                    cancelled.set()
                    await retired.wait()
                    raise TransportError(late_code, "retired lease", False) from None
            return {"lease_id": lease_id}

        class HeldConnect(ScriptedTerminalSocket):
            async def connect(self) -> None:
                await super().connect()
                connecting.set()
                await connected.wait()

        sockets: list[ScriptedTerminalSocket] = []

        def open_stream(*_: Any) -> ScriptedTerminalSocket:
            sock = ScriptedTerminalSocket() if not sockets else HeldConnect()
            sockets.append(sock)
            return sock

        scripted, session = await opened(
            "agent" if operation == "agent" else "shell", renew, open_stream
        )
        first_id.append(session.lease.lease_id)
        session.start_lease_renewal(interval_s=0.001)
        await entered.wait()
        old_controller, old_driver = session.controller, session._renewal
        callback = old_driver._on_terminal_failure
        task = asyncio.create_task(replacement(session, operation))
        try:
            await until(lambda: cancelled.is_set() or connecting.is_set())
            assert cancelled.is_set(), "replacement raced the old renewal driver"
            assert not connecting.is_set() and not sockets[0].closed
            assert session.controller is old_controller and session._renewal is None
            retired.set()
            await connecting.wait()
            new_controller, new_lease = session.controller, session.lease
            assert new_controller is not old_controller and new_lease.lease_id != first_id[0]
            assert not old_driver.running
            callback(late_code)  # Delayed delivery from the old driver during new connect.
            assert not new_controller.writer_revoked and not session.notice
            delivered = next(
                record.getMessage()
                for record in caplog.records
                if "event=renew_callback" in record.getMessage()
                and f"current_lease={new_lease.lease_id}" in record.getMessage()
            )
            assert f"lease={first_id[0]} current_lease={new_lease.lease_id}" in delivered
            assert "reason=superseded" in delivered
            connected.set()
            assert await task == "live"
            assert session.controller is new_controller and session._renewal is not old_driver
            assert session.keys_enabled
            assert scripted.submitted == [] and scripted.prompted == []
            assert all(kind != "bytes" for sock in sockets for kind, _ in sock.sent)
            if operation == "agent":
                assert session.composer_props()["draft"] == "exact unsent\ndraft"
                assert not session._auth_revoked
        finally:
            retired.set()
            connected.set()
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            await session.dispose()

    asyncio.run(scenario())


@pytest.mark.parametrize("operation", ["agent", "shell-takeover", "shell-reconnect"])
def test_cancelled_retirement_never_attaches_or_replaces(operation: str) -> None:
    async def scenario() -> None:
        entered, cancelled, retired = (asyncio.Event() for _ in range(3))

        async def renew(_: str) -> dict[str, Any]:
            entered.set()
            try:
                await asyncio.Event().wait()
            finally:
                cancelled.set()
                await retired.wait()
            return {}

        scripted, session = await opened("agent" if operation == "agent" else "shell", renew)
        session.start_lease_renewal(interval_s=0.001)
        await entered.wait()
        old_controller, old_driver = session.controller, session._renewal
        before = (len(scripted.ops.attaches), len(scripted.ops.leases))
        task = asyncio.create_task(replacement(session, operation))
        try:
            await until(lambda: cancelled.is_set() or task.done())
            assert cancelled.is_set()
            task.cancel()
            retired.set()
            with pytest.raises(asyncio.CancelledError):
                await task
            assert (len(scripted.ops.attaches), len(scripted.ops.leases)) == before
            assert session.controller is old_controller and not old_driver.running
            old_driver._on_terminal_failure("lease_expired")
            assert not old_controller.writer_revoked
        finally:
            retired.set()
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            await session.dispose()

    asyncio.run(scenario())


@pytest.mark.parametrize("kind", ["agent", "shell"])
@pytest.mark.parametrize("code", ["lease_expired", "unauthenticated", "forbidden"])
def test_current_renewal_failure_still_revokes_and_keeps_draft(kind: str, code: str) -> None:
    async def scenario() -> None:
        async def renew(_: str) -> dict[str, Any]:
            raise TransportError(code, "current denial", False)

        _, session = await opened(kind, renew)
        try:
            session.start_lease_renewal(interval_s=0.001)
            await until(lambda: session.controller.writer_revoked)
            assert not session.keys_enabled and session._renewal.invalidated
            if kind == "agent":
                assert session.composer_props()["draft"] == "exact unsent\ndraft"
                if code in ("unauthenticated", "forbidden"):
                    assert session._auth_revoked
                    assert await session.on_send() == "ignored"
        finally:
            await session.dispose()

    asyncio.run(scenario())


@pytest.mark.parametrize("operation", ["agent", "shell-takeover", "shell-reconnect"])
def test_generation_change_during_retirement_never_replaces(operation: str) -> None:
    async def scenario() -> None:
        entered, cancelled, retired = (asyncio.Event() for _ in range(3))

        async def renew(_: str) -> dict[str, Any]:
            entered.set()
            try:
                await asyncio.Event().wait()
            finally:
                cancelled.set()
                await retired.wait()
            return {}

        scripted, session = await opened("agent" if operation == "agent" else "shell", renew)
        session.start_lease_renewal(interval_s=0.001)
        await entered.wait()
        old, driver = session.controller, session._renewal
        before = (len(scripted.ops.attaches), len(scripted.ops.leases))
        task = asyncio.create_task(replacement(session, operation))
        try:
            await cancelled.wait()
            session.suspend_input()
            retired.set()
            await task
            assert session.controller is old and old.writer_paused
            assert (len(scripted.ops.attaches), len(scripted.ops.leases)) == before
            assert not driver.running
            driver._on_terminal_failure("forbidden")
            assert not old.writer_revoked
        finally:
            retired.set()
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            await session.dispose()

    asyncio.run(scenario())
