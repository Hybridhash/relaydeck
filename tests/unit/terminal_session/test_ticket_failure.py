"""A failing ticket after a granted lease keeps the lease held (UI resume
re-tickets it), logs one content-free line naming the code, keeps the same
user notice, and a later tap still opens normally. Agent and shell."""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from relaydeck.client.features.shell.ops import (
    ScriptedShellGatewayOps,
    ShellOpError,
)
from relaydeck.client.features.shell.session import (
    SHELL_DISCONNECTED,
    SHELL_LIVE,
)
from relaydeck.client.features.terminal.agent_session import (
    AGENT_DISCONNECTED,
    AGENT_LIVE,
)
from tests.support.terminal.sessions import _agent_session, _session

LOG = "relaydeck.client.typing_split"


class _Capture(logging.Handler):
    def __init__(self) -> None:
        super().__init__()
        self.messages: list[str] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.messages.append(record.getMessage())


def _fail_ticket_once(ops: ScriptedShellGatewayOps, code: str) -> None:
    real_ticket = ops.terminal_ticket
    failed = False

    async def fail_once(lease_id: str) -> str:
        nonlocal failed
        if not failed:
            failed = True
            raise ShellOpError(code, "boom")
        return await real_ticket(lease_id)

    ops.terminal_ticket = fail_once  # type: ignore[method-assign]


def _ticket_lines(messages: list[str]) -> list[str]:
    return [m for m in messages if "ticket_failed" in m]


def test_agent_ticket_failure_releases_and_recovers() -> None:
    async def scenario() -> None:
        session, ops, _factory, _submitted = _agent_session()
        granted: list[str] = []
        real_acquire = ops.acquire_lease

        async def record_acquire(attachment_id: str, mode: str) -> Any:
            lease = await real_acquire(attachment_id, mode)
            granted.append(lease.lease_id)
            return lease

        ops.acquire_lease = record_acquire  # type: ignore[method-assign]
        _fail_ticket_once(ops, "host_offline")
        logger = logging.getLogger(LOG)
        capture = _Capture()
        logger.addHandler(capture)
        logger.setLevel(logging.INFO)
        try:
            session.open()
            phase = await session.confirm_takeover()
            assert phase == AGENT_DISCONNECTED
            assert session.view().notice == "Could not stream (host_offline)."
            assert ops.releases == []
            assert session.lease is not None
            assert session.lease.lease_id == granted[0]
            assert _ticket_lines(capture.messages) == ["ticket_failed code=host_offline"]
            session.open()
            assert await session.confirm_takeover() == AGENT_LIVE
            assert ops.releases == []
        finally:
            logger.removeHandler(capture)

    asyncio.run(scenario())


def test_shell_open_ticket_failure_releases_and_recovers() -> None:
    async def scenario() -> None:
        session, ops, _factory = _session()
        granted: list[str] = []
        real_acquire = ops.acquire_lease

        async def record_acquire(attachment_id: str, mode: str) -> Any:
            lease = await real_acquire(attachment_id, mode)
            granted.append(lease.lease_id)
            return lease

        ops.acquire_lease = record_acquire  # type: ignore[method-assign]
        _fail_ticket_once(ops, "host_offline")
        logger = logging.getLogger(LOG)
        capture = _Capture()
        logger.addHandler(capture)
        logger.setLevel(logging.INFO)
        try:
            assert await session.open("term-1") == SHELL_DISCONNECTED
            assert session.view().notice == "Could not stream (host_offline)."
            assert ops.releases == []
            assert session.lease is not None
            assert session.lease.lease_id == granted[0]
            assert _ticket_lines(capture.messages) == ["ticket_failed code=host_offline"]
            # A later tap re-attaches (takeover: the scripted fake treats a
            # second plain attach as a conflict) and streams normally.
            assert await session.take_over() == SHELL_LIVE
            assert ops.releases == []
        finally:
            logger.removeHandler(capture)

    asyncio.run(scenario())


def test_shell_takeover_ticket_failure_releases() -> None:
    async def scenario() -> None:
        session, ops, _factory = _session()
        assert await session.open("term-1") == SHELL_LIVE
        granted: list[str] = []
        real_acquire = ops.acquire_lease

        async def record_acquire(attachment_id: str, mode: str) -> Any:
            lease = await real_acquire(attachment_id, mode)
            granted.append(lease.lease_id)
            return lease

        ops.acquire_lease = record_acquire  # type: ignore[method-assign]
        _fail_ticket_once(ops, "host_offline")
        logger = logging.getLogger(LOG)
        capture = _Capture()
        logger.addHandler(capture)
        logger.setLevel(logging.INFO)
        try:
            assert await session.take_over() == SHELL_DISCONNECTED
            assert session.view().notice == "Could not stream (host_offline)."
            assert granted[-1] not in ops.releases
            assert session.lease is not None
            assert session.lease.lease_id == granted[-1]
            assert _ticket_lines(capture.messages) == ["ticket_failed code=host_offline"]
            assert await session.take_over() == SHELL_LIVE
        finally:
            logger.removeHandler(capture)

    asyncio.run(scenario())


def test_shell_reconnect_ticket_failure_releases() -> None:
    async def scenario() -> None:
        session, ops, _factory = _session()
        assert await session.open("term-1") == SHELL_LIVE
        granted: list[str] = []
        real_acquire = ops.acquire_lease

        async def record_acquire(attachment_id: str, mode: str) -> Any:
            lease = await real_acquire(attachment_id, mode)
            granted.append(lease.lease_id)
            return lease

        ops.acquire_lease = record_acquire  # type: ignore[method-assign]
        _fail_ticket_once(ops, "host_offline")
        logger = logging.getLogger(LOG)
        capture = _Capture()
        logger.addHandler(capture)
        logger.setLevel(logging.INFO)
        try:
            assert await session.reconnect() == SHELL_DISCONNECTED
            assert session.view().notice == "Could not stream (host_offline)."
            assert granted[-1] not in ops.releases
            assert session.lease is not None
            assert session.lease.lease_id == granted[-1]
            assert _ticket_lines(capture.messages) == ["ticket_failed code=host_offline"]
            assert await session.reconnect() == SHELL_LIVE
        finally:
            logger.removeHandler(capture)

    asyncio.run(scenario())
