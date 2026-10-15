"""Full session paths.

- A 12 MiB sustained-output probe through the REAL
  AgentSession/ShellSession stream path stays bounded (output_log is a
  ring-only unit test's blind spot).
- The xterm2 engine reports its live bracketed-paste mode
  over MODE_REPORT after every write/reset, and the next reviewed paste
  frames accordingly; output bytes are never re-parsed for 2004
  (a regex re-parse provably diverges from the engine across RIS and
  split private-mode params). Paste review keeps its verbatim counts
  and never implies Enter.
- The mounted live session renews its control lease while
  foreground, terminal renewal failure invalidates the writer with a
  visible notice, and pause/dispose cancel renewal.
"""

from __future__ import annotations

import asyncio
from collections import deque
from typing import Any

from relaydeck_terminal import protocol
from relaydeck_terminal.terminal import RelayDeckTerminal

from relaydeck.client.adapters.fake import FakeClipboard
from relaydeck.client.features.shell import ScriptedShellGatewayOps, ShellSession
from relaydeck.client.features.sync.transport import TerminalSocket, TransportError
from relaydeck.client.features.terminal.agent_session import AGENT_LIVE, AgentSession
from relaydeck.client.features.terminal.output_log import OUTPUT_LOG_LIMIT
from relaydeck.client.features.terminal.paste_policy import BRACKET_END, BRACKET_START
from relaydeck.gateway import terminal_wire
from tests.support.host_harness import run

PROBE_BYTES = 12 * 1024 * 1024


class _ScriptedSocket(TerminalSocket):
    def __init__(self, script: deque[tuple[str, Any]] | None = None) -> None:
        self._script = script if script is not None else deque()
        self.sent_bytes: list[bytes] = []
        self._wake = asyncio.Event()

    async def connect(self) -> None:
        pass

    async def send_bytes(self, data: bytes) -> None:
        self.sent_bytes.append(data)

    async def send_text(self, data: str) -> None:
        pass

    async def receive(self) -> tuple[str, Any]:
        if self._script:
            return self._script.popleft()
        await self._wake.wait()
        raise TransportError("host_offline", "socket closed")

    async def close(self) -> None:
        self._wake.set()


class _SocketFactory:
    def __init__(self) -> None:
        self.sockets: list[_ScriptedSocket] = []
        self.scripts: deque[deque[tuple[str, Any]]] = deque()

    def queue(self, *frames: tuple[str, Any]) -> None:
        self.scripts.append(deque(frames))

    def __call__(self, ticket: str) -> TerminalSocket:
        script = self.scripts.popleft() if self.scripts else deque()
        sock = _ScriptedSocket(script)
        self.sockets.append(sock)
        return sock


def _agent_session(
    factory: _SocketFactory,
    renew: Any = None,
) -> tuple[AgentSession, list[dict[str, Any]]]:
    ops = ScriptedShellGatewayOps()

    async def submit(command: dict[str, Any]) -> dict[str, Any]:
        return {"command_id": command["command_id"]}

    async def prompt(target: str, text: str) -> dict[str, Any]:
        return {"ok": True}

    session = AgentSession(
        ops=ops,
        host_id="home-mac",
        device_id=ops.device_id,
        socket_factory=factory,
        submit_command=submit,
        prompt_text=prompt,
        host_epoch="epoch-1",
        agent_id="agent-1",
        target="sess:1",
        initial_status="idle",
        clipboard=FakeClipboard(),
        renew_lease=renew,
    )
    return session, []


def _shell_session(factory: _SocketFactory, renew: Any = None) -> ShellSession:
    ops = ScriptedShellGatewayOps()
    return ShellSession(
        ops=ops,
        host_id="home-mac",
        device_id=ops.device_id,
        socket_factory=factory,
        clipboard=FakeClipboard(),
        renew_lease=renew,
    )


def _output_frames(total: int, chunk: int) -> list[tuple[str, Any]]:
    frames: list[tuple[str, Any]] = []
    offset = 0
    marker = 0
    while offset < total:
        size = min(chunk, total - offset)
        payload = bytes((marker + i) % 251 for i in range(size))
        frames.append(("bytes", terminal_wire.encode_output("e1", offset, payload)))
        offset += size
        marker += size
    return frames


async def _cancel(task: asyncio.Task[Any]) -> None:
    task.cancel()
    try:
        await task
    except (asyncio.CancelledError, TransportError):
        pass


async def _wait_for(predicate: Any, timeout: float = 30.0) -> None:
    deadline = asyncio.get_running_loop().time() + timeout
    while not predicate():
        if asyncio.get_running_loop().time() > deadline:
            raise AssertionError("timed out waiting for condition")
        await asyncio.sleep(0.01)


def test_agent_output_log_bounded_on_full_session_path() -> None:
    async def scenario() -> None:
        factory = _SocketFactory()
        frames = _output_frames(PROBE_BYTES, 64 * 1024)
        assert len(frames) == PROBE_BYTES // (64 * 1024)
        factory.queue(*frames)
        session, _ = _agent_session(factory)
        session.open()
        assert await session.confirm_takeover() == AGENT_LIVE
        task = asyncio.create_task(session.run())
        try:
            await _wait_for(
                lambda: session.controller is not None
                and session.term_state.applied_offset >= PROBE_BYTES
            )
        finally:
            await _cancel(task)
        assert len(session.output_log) <= OUTPUT_LOG_LIMIT
        assert session.output_log.dropped_bytes == PROBE_BYTES - len(session.output_log)
        # Newest bytes survive: the tail matches the probe tail.
        tail = bytes(session.output_log)
        assert len(tail) == OUTPUT_LOG_LIMIT
        expected_marker = PROBE_BYTES - OUTPUT_LOG_LIMIT
        assert tail[:8] == bytes((expected_marker + i) % 251 for i in range(8))

    run(scenario())


def test_shell_output_log_bounded_on_full_session_path() -> None:
    async def scenario() -> None:
        factory = _SocketFactory()
        factory.queue(*_output_frames(PROBE_BYTES, 64 * 1024))
        session = _shell_session(factory)
        assert await session.open("term-1") == "live"
        task = asyncio.create_task(session.run())
        try:
            await _wait_for(lambda: session.term_state.applied_offset >= PROBE_BYTES)
        finally:
            await _cancel(task)
        assert len(session.output_log) <= OUTPUT_LOG_LIMIT
        assert session.output_log.dropped_bytes == PROBE_BYTES - len(session.output_log)

    run(scenario())


def test_engine_mode_report_drives_paste_framing() -> None:
    async def scenario() -> None:
        factory = _SocketFactory()
        factory.queue(
            ("bytes", terminal_wire.encode_output("e1", 0, b"\x1b[?2004hready $ ")),
        )
        session, _ = _agent_session(factory)
        assert session.bracketed_paste is False
        session.open()
        assert await session.confirm_takeover() == AGENT_LIVE
        # Output bytes are NEVER re-parsed for 2004: the DECSET above
        # streams through the real session path without moving the flag
        # (a regex here provably diverged from the engine across RIS
        # and split private-mode params).
        task = asyncio.create_task(session.run())
        try:
            await _wait_for(lambda: session.term_state.applied_offset > 0)
        finally:
            await _cancel(task)
        assert session.bracketed_paste is False
        # The ENGINE reports its live mode over the bridge instead, wired
        # exactly like the mount (mode listener -> set_bracketed_paste).
        term = RelayDeckTerminal()
        term.add_mode_listener(session.set_bracketed_paste)
        term._test_inject_dart_message(protocol.encode_mode_report(True))
        assert session.bracketed_paste is True
        # The reviewed paste frames because the LIVE engine negotiated it.
        assert await session.request_paste("a\nb") == "review"
        review = session.pending_review
        assert review is not None
        assert (review.line_count, review.character_count) == (2, 3)
        assert review.preview == "a\nb"
        assert await session.confirm_paste() is True
        chunk = terminal_wire.decode_input(factory.sockets[0].sent_bytes[0])
        assert chunk.data == BRACKET_START + b"a\nb" + BRACKET_END
        # And the engine can negotiate it back off.
        term._test_inject_dart_message(protocol.encode_mode_report(False))
        assert session.bracketed_paste is False
        # A takeover resets to unnegotiated; the post-reset engine report
        # re-establishes the truth.
        term._test_inject_dart_message(protocol.encode_mode_report(True))
        assert session.bracketed_paste is True
        session.open()
        assert await session.confirm_takeover() == AGENT_LIVE
        assert session.bracketed_paste is False

    run(scenario())


def test_live_session_renews_lease_while_foreground() -> None:
    async def scenario() -> None:
        renewals: list[str] = []

        async def renew(lease_id: str) -> dict[str, Any]:
            renewals.append(lease_id)
            return {"lease_id": lease_id, "expires_at": "2030-01-01T00:00:00Z"}

        factory = _SocketFactory()
        session, _ = _agent_session(factory, renew=renew)
        session.open()
        assert await session.confirm_takeover() == AGENT_LIVE
        assert session.lease is not None
        session.start_lease_renewal(interval_s=0.01)
        await asyncio.sleep(0.05)
        assert len(renewals) >= 2
        assert set(renewals) == {session.lease.lease_id}
        await session.stop_lease_renewal()
        count = len(renewals)
        await asyncio.sleep(0.03)
        assert len(renewals) == count

    run(scenario())


def test_terminal_renewal_failure_invalidates_writer_loudly() -> None:
    async def scenario() -> None:
        async def renew(lease_id: str) -> dict[str, Any]:
            raise TransportError("lease_conflict", "held elsewhere")

        factory = _SocketFactory()
        factory2 = factory
        session = _shell_session(factory2, renew=renew)
        assert await session.open("term-1") == "live"
        assert session.keys_enabled is True
        session.start_lease_renewal(interval_s=0.01)
        await _wait_for(lambda: session.keys_enabled is False, timeout=5.0)
        assert "lease" in session.notice.lower()
        # Invalidated: input fails closed and renewal stops firing.
        assert await session.press_key("a") is False
        await session.stop_lease_renewal()

    run(scenario())


def test_pause_cancels_renewal_and_dispose_ends_it() -> None:
    async def scenario() -> None:
        renewals: list[str] = []

        async def renew(lease_id: str) -> dict[str, Any]:
            renewals.append(lease_id)
            return {"lease_id": lease_id}

        factory = _SocketFactory()
        session = _shell_session(factory, renew=renew)
        assert await session.open("term-1") == "live"
        session.start_lease_renewal(interval_s=0.01)
        await asyncio.sleep(0.03)
        assert len(renewals) >= 1
        await session.pause()
        count = len(renewals)
        await asyncio.sleep(0.03)
        assert len(renewals) == count  # pause cancels the task
        await session.dispose()
        assert session.disposed is True

    run(scenario())


def test_token_loss_revokes_session_writer_loudly() -> None:
    """The exact `_token` failure triple through the full agent path:
    one renew call, writer revoked, sign-in notice — never retried."""

    async def scenario() -> None:
        renewals: list[str] = []

        async def renew(lease_id: str) -> dict[str, Any]:
            renewals.append(lease_id)
            raise TransportError("unauthenticated", "no access token", False)

        factory = _SocketFactory()
        session, _ = _agent_session(factory, renew=renew)
        session.open()
        assert await session.confirm_takeover() == AGENT_LIVE
        assert session.keys_enabled is True
        session.start_lease_renewal(interval_s=0.005)
        await _wait_for(lambda: session.keys_enabled is False, timeout=5.0)
        assert session.lease is not None
        assert renewals == [session.lease.lease_id]
        assert len(renewals) == 1
        assert "sign in" in session.notice
        assert session.controller is not None
        assert session.controller.writer_revoked is True
        assert await session.press_key("enter") is False
        count = len(renewals)
        await asyncio.sleep(0.03)
        assert len(renewals) == count  # stopped: no retry
        await session.stop_lease_renewal()

    run(scenario())


def test_saved_send_after_dispose_cannot_submit() -> None:
    """A composer `on_send` saved before dispose must
    fail closed afterwards — no agent.prompt, draft preserved, no replay."""

    async def scenario() -> None:
        submitted: list[dict[str, Any]] = []
        ops = ScriptedShellGatewayOps()

        async def submit(command: dict[str, Any]) -> dict[str, Any]:
            submitted.append(command)
            return {"command_id": command["command_id"]}

        async def prompt(target: str, text: str) -> dict[str, Any]:
            return {"ok": True}

        factory = _SocketFactory()
        session = AgentSession(
            ops=ops,
            host_id="home-mac",
            device_id=ops.device_id,
            socket_factory=factory,
            submit_command=submit,
            prompt_text=prompt,
            host_epoch="epoch-1",
            agent_id="agent-1",
            target="sess:1",
            initial_status="idle",
            clipboard=FakeClipboard(),
        )
        session.open()
        assert await session.confirm_takeover() == AGENT_LIVE
        session.on_draft_change("stale draft")
        saved = session.composer_callbacks()["on_send"]
        saved_confirm = session.composer_callbacks()["on_confirm_blocked"]
        await session.dispose()
        assert session.disposed is True
        assert await saved() == "ignored"
        assert await saved_confirm() is False
        assert submitted == []
        assert session.store.draft == "stale draft"
        # Direct calls fail closed too.
        assert await session.on_send() == "ignored"
        assert submitted == []

    run(scenario())


def test_saved_send_after_disconnect_cannot_submit() -> None:
    """Switch/Back path: disconnect stales saved composer closures."""

    async def scenario() -> None:
        factory = _SocketFactory()
        session, _ = _agent_session(factory)
        session.open()
        assert await session.confirm_takeover() == AGENT_LIVE
        session.on_draft_change("kept")
        saved = session.composer_callbacks()["on_send"]
        await session.disconnect()
        assert await saved() == "ignored"
        assert session.store.draft == "kept"

    run(scenario())


def test_retryable_failures_end_at_lease_expiry() -> None:
    """Retryable renewal failures retry while the lease is live, then the
    session writer dies exactly at the known expiry with no further
    renew calls."""

    async def scenario() -> None:
        from dataclasses import replace

        from relaydeck.client.features.shell.ops import LeaseInfo
        from relaydeck.client.features.terminal.renewal import utc_stamp_in

        class _ShortLeaseOps(ScriptedShellGatewayOps):
            async def acquire_lease(self, attachment_id: str, mode: str) -> LeaseInfo:
                lease = await super().acquire_lease(attachment_id, mode)
                return replace(lease, expires_at=utc_stamp_in(0.15))

        renewals: list[str] = []
        ops = _ShortLeaseOps()

        async def renew(lease_id: str) -> dict[str, Any]:
            renewals.append(lease_id)
            raise TransportError("host_offline", "tunnel down", True)

        factory = _SocketFactory()
        session = ShellSession(
            ops=ops,
            host_id="home-mac",
            device_id=ops.device_id,
            socket_factory=factory,
            clipboard=FakeClipboard(),
            renew_lease=renew,
        )
        assert await session.open("term-1") == "live"
        assert session.keys_enabled is True
        session.start_lease_renewal(interval_s=0.005)
        await _wait_for(lambda: session.keys_enabled is False, timeout=5.0)
        assert "expired" in session.notice
        assert len(renewals) >= 1
        count = len(renewals)
        await asyncio.sleep(0.04)
        assert len(renewals) == count  # no calls at/after expiry
        await session.stop_lease_renewal()

    run(scenario())
