"""Android lifecycle and connectivity.

Pause-invalidates-writer, ordered resume, process-death recovery, lease
release/expiry, minimal route restoration, and network-switch
resynchronization — all proven headless. Real-hardware scenarios stay
blocked (they need real hardware).
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncGenerator
from pathlib import Path as FsPath
from typing import Any

import pytest

from relaydeck.client.adapters.ports import ConnectivityState
from relaydeck.client.features.composer.domain import (
    LOST_DELIVERY_MESSAGE,
    REVIEW_LOST_MESSAGE,
    SEND_BLOCKED_REVIEW,
    SUBMISSION_SENDING,
    SUBMISSION_UNKNOWN,
    ComposerStore,
)
from relaydeck.client.features.inventory.routing import RouteState
from relaydeck.client.features.lifecycle import (
    FORCE_STOP_LIMITS,
    LINK_EVENT_HISTORY,
    OUTAGE_MESSAGE,
    PAUSE_RULE,
    RESUME_ORDER,
    STORAGE_POLICY,
    AppPhase,
    CursorDeduper,
    DeathReport,
    DurableStores,
    LifecycleError,
    LifecycleMachine,
    LinkResync,
    ProcessMemory,
    ResumeError,
    ResumeOrchestrator,
    ResumeReport,
    StaleWriterError,
    WriterGate,
    audit_stores,
    draft_key,
    pause_persist,
    restore_minimal_route,
    resume_restore,
    simulate_config_change,
    simulate_death,
)
from relaydeck.client.features.lifecycle.machine import CONFIG_KEYBOARD, CONFIG_ROTATE
from relaydeck.client.features.shell.ops import (
    ScriptedShellGatewayOps,
    ShellOpError,
)
from relaydeck.client.features.shell.session import (
    SHELL_CONFLICT,
    SHELL_DISCONNECTED,
    SHELL_EXITED,
    SHELL_LIVE,
    ShellSession,
)
from relaydeck.client.features.sync.backoff import Backoff
from relaydeck.client.features.sync.controller import SyncController
from relaydeck.client.features.sync.sse import SseFrame
from relaydeck.client.features.sync.transport import (
    GatewayTransport,
    TransportError,
)
from relaydeck.client.features.terminal.agent_session import (
    AGENT_DISCONNECTED,
    AGENT_EXITED,
    AGENT_LIVE,
    AgentSession,
)
from relaydeck.client.features.terminal.controller import TerminalController
from relaydeck.client.features.terminal.keys import KeyInputError
from relaydeck.client.features.terminal.paste_policy import PasteError
from relaydeck.client.features.terminal.paste_review import REQUEST_REVIEW, STALE_MESSAGE
from relaydeck.contracts.wire import terminal as terminal_wire
from relaydeck.domain.models import ConnectionState
from relaydeck.gateway.backend import FakeBackend
from relaydeck.gateway.leases import CONTROL_LEASE_TTL_MS
from relaydeck.gateway.ring import STATUS_OK
from tests.support.fakes import FakeClipboard, FakeConnectivity, FakeCredentialStore
from tests.support.sync.lifecycle import _agent, _shell, _SocketFactory

FIXTURES = FsPath(__file__).resolve().parent.parent / "fixtures" / "gateway"


# -- shared fakes -----------------------------------------------------------


def _output(epoch: str, offset: int, data: bytes) -> tuple[str, Any]:
    return ("bytes", terminal_wire.encode_output(epoch, offset, data))


def _snapshot_doc(cursor: str = "c0") -> dict[str, Any]:
    doc: dict[str, Any] = json.loads((FIXTURES / "snapshot.json").read_text())
    doc["host"]["id"] = "h1"
    doc["host"]["epoch"] = "e1"
    for key in ("workspaces", "tabs", "panes", "agents"):
        for item in doc[key]:
            item["host_id"] = "h1"
    doc["cursor"] = cursor
    return doc


def _event_doc(seq: int) -> dict[str, Any]:
    doc: dict[str, Any] = json.loads((FIXTURES / "metadata-event.json").read_text())
    doc.update(
        {
            "event_id": f"ev-{seq}",
            "host_id": "h1",
            "host_epoch": "e1",
            "sequence": seq,
            "cursor": f"c{seq}",
        }
    )
    return doc


def _meta(seq: int) -> SseFrame:
    return SseFrame("metadata", json.dumps(_event_doc(seq)))


class _LifecycleTransport(GatewayTransport):
    """Scripted transport with parkable streams for mid-stream switches."""

    def __init__(self) -> None:
        self.snapshots = 0
        self.cursors: list[str] = []
        self.finished = False
        self.scripts: list[list[Any]] = []

    async def me(self) -> dict[str, Any]:
        raise NotImplementedError

    async def hosts(self) -> dict[str, Any]:
        raise NotImplementedError

    async def agents(
        self,
        host_id: str,
        limit: int = 50,
        page_token: str = "",
        *,
        project: str = "",
        status: str = "",
        query: str = "",
    ) -> dict[str, Any]:
        raise NotImplementedError

    async def snapshot(self, host_id: str) -> dict[str, Any]:
        self.snapshots += 1
        assert host_id == "h1"
        return _snapshot_doc()

    async def stream(self, host_id: str, cursor: str) -> AsyncGenerator[SseFrame]:
        self.cursors.append(cursor)
        script = self.scripts.pop(0) if self.scripts else []
        for item in script:
            if isinstance(item, TransportError):
                raise item
            if isinstance(item, asyncio.Event):
                await item.wait()
                continue
            yield item
            await asyncio.sleep(0)
        self.finished = not self.scripts

    async def submit_command(self, command: dict[str, Any]) -> dict[str, Any]:
        raise NotImplementedError

    async def command_status(self, host_id: str, command_id: str) -> dict[str, Any] | None:
        raise NotImplementedError

    async def terminal_ticket(self, lease_id: str) -> dict[str, Any]:
        raise NotImplementedError


async def _no_sleep(_: float) -> None:
    return None


async def _wait_until(pred: Any, timeout: float = 10.0) -> None:
    deadline = asyncio.get_running_loop().time() + timeout
    while not pred():
        if asyncio.get_running_loop().time() > deadline:
            raise AssertionError("timed out waiting for sync condition")
        await asyncio.sleep(0.01)


# -- machine: states + transitions -------------------------------------------


def test_machine_pause_then_resume_order() -> None:
    machine = LifecycleMachine()
    assert machine.phase is AppPhase.FOREGROUND
    assert machine.on_background() is AppPhase.BACKGROUND
    assert machine.apply_pause_policy() is AppPhase.PAUSED
    assert machine.on_resume_complete() is AppPhase.FOREGROUND
    assert machine.transitions == [
        ("foreground", "background", "background"),
        ("background", "paused", "pause_policy"),
        ("paused", "foreground", "resume"),
    ]


def test_machine_resume_from_background_without_pause() -> None:
    machine = LifecycleMachine()
    machine.on_background()
    assert machine.on_resume_complete() is AppPhase.FOREGROUND


def test_machine_death_and_cold_start() -> None:
    for start in (
        lambda m: None,
        lambda m: m.on_background(),
        lambda m: (m.on_background(), m.apply_pause_policy()),
    ):
        machine = LifecycleMachine()
        start(machine)
        assert machine.on_process_death() is AppPhase.DEAD
        assert machine.on_cold_start_complete() is AppPhase.FOREGROUND


def test_machine_illegal_transitions_raise() -> None:
    machine = LifecycleMachine()
    with pytest.raises(LifecycleError):
        machine.apply_pause_policy()  # pause policy needs background first
    with pytest.raises(LifecycleError):
        machine.on_resume_complete()  # already foreground
    with pytest.raises(LifecycleError):
        machine.on_cold_start_complete()  # not dead
    machine.on_background()
    with pytest.raises(LifecycleError):
        machine.on_background()  # already background
    with pytest.raises(LifecycleError):
        machine.on_cold_start_complete()  # not dead
    machine.on_process_death()
    with pytest.raises(LifecycleError):
        machine.on_process_death()  # already dead
    with pytest.raises(LifecycleError):
        machine.on_resume_complete()  # dead needs cold start, not resume
    with pytest.raises(LifecycleError):
        machine.config_change(CONFIG_ROTATE)  # a dead process observes nothing


def test_machine_config_change_is_not_a_transition() -> None:
    machine = LifecycleMachine()
    machine.on_background()
    change = machine.config_change(CONFIG_ROTATE)
    assert change.phase is AppPhase.BACKGROUND
    assert machine.phase is AppPhase.BACKGROUND
    change = machine.config_change(CONFIG_KEYBOARD)
    assert change.kind == CONFIG_KEYBOARD
    assert machine.phase is AppPhase.BACKGROUND
    assert machine.transitions == [("foreground", "background", "background")]
    with pytest.raises(LifecycleError):
        machine.config_change("locale")


# -- writer gate: pause-invalidates-writer ------------------------------------


def test_pause_rule_text_is_exact() -> None:
    assert "PAUSE invalidates the control writer immediately" in PAUSE_RULE
    assert "paused writer can never emit again under its old binding" in PAUSE_RULE
    assert "re-acquires explicitly" in PAUSE_RULE
    assert "never silently steal" in PAUSE_RULE
    assert "never auto-resent" in PAUSE_RULE


def test_writer_gate_pause_retires_emit_fails() -> None:
    async def scenario() -> None:
        sent: list[bytes] = []

        async def sender(data: bytes) -> None:
            sent.append(data)

        gate = WriterGate()
        binding = gate.acquire(7, sender)
        assert gate.is_live is True
        await gate.emit(binding, b"before-pause")
        assert sent == [b"before-pause"]

        gate.pause()
        assert gate.is_live is False
        with pytest.raises(StaleWriterError) as paused:
            await gate.emit(binding, b"after-pause")
        assert paused.value.reason == "paused"
        assert sent == [b"before-pause"]  # nothing delivered

        gate.pause()  # idempotent
        with pytest.raises(StaleWriterError):
            await gate.emit(binding, b"still-paused")
        assert sent == [b"before-pause"]

    asyncio.run(scenario())


def test_writer_gate_resume_reacquires_fresh_binding_no_steal() -> None:
    async def scenario() -> None:
        sent: list[bytes] = []

        async def sender(data: bytes) -> None:
            sent.append(data)

        gate = WriterGate()
        old = gate.acquire(7, sender)
        gate.pause()
        # Explicit re-acquire mints a FRESH binding; the retired one can
        # never emit again even though the gate is live.
        fresh = gate.acquire(7, sender)
        assert fresh != old
        assert gate.is_live is True
        with pytest.raises(StaleWriterError) as stale:
            await gate.emit(old, b"stale-handle")
        assert stale.value.reason == "superseded"
        await gate.emit(fresh, b"resumed")
        assert sent == [b"resumed"]

    asyncio.run(scenario())


def test_writer_gate_drop_forgets_sender() -> None:
    async def scenario() -> None:
        sent: list[bytes] = []

        async def sender(data: bytes) -> None:
            sent.append(data)

        gate = WriterGate()
        binding = gate.acquire(3, sender)
        gate.drop()
        with pytest.raises(StaleWriterError) as dead:
            await gate.emit(binding, b"post-death")
        assert dead.value.reason == "dead"
        assert sent == []

    asyncio.run(scenario())


def test_controller_pause_hook_blocks_all_sends() -> None:
    async def scenario() -> None:
        factory = _SocketFactory()
        controller = TerminalController(
            socket_factory=factory,
            lease_id="lease-1",
            generation=4,
            mode="control",
            on_bytes=lambda data: None,
        )
        await controller.start("ticket-1", "", 0)
        assert controller.paste_generation() == 4
        assert controller.key_generation() == 4
        await controller.send_paste_chunk(b"live")
        assert len(factory.sockets) == 1
        assert len(factory.sockets[0].sent_bytes) == 1

        controller.pause_writer()
        assert controller.writer_paused is True
        assert controller.paste_generation() is None
        assert controller.key_generation() is None
        with pytest.raises(PasteError) as paste:
            await controller.send_paste_chunk(b"paused-paste")
        assert paste.value.code == "no_writer"
        with pytest.raises(KeyInputError) as key:
            await controller.send_key_bytes(b"paused-key")
        assert key.value.code == "no_writer"
        assert len(factory.sockets[0].sent_bytes) == 1  # nothing delivered

        controller.pause_writer()  # idempotent
        assert controller.paste_generation() is None

        # Explicit re-enable restores THIS controller's probe only: the
        # generation and lease are unchanged (no steal).
        controller.resume_writer()
        assert controller.writer_paused is False
        assert controller.paste_generation() == 4
        await controller.send_paste_chunk(b"resumed")
        assert len(factory.sockets[0].sent_bytes) == 2

    asyncio.run(scenario())


# -- resume: order, control last ---------------------------------------


def _orchestrator(
    calls: list[str],
    *,
    fail_at: str = "",
    statuses: dict[str, dict[str, Any] | None] | None = None,
) -> ResumeOrchestrator:
    async def refresh_identity() -> dict[str, Any]:
        calls.append("refresh_identity")
        if fail_at == "refresh_identity":
            raise TransportError("forbidden", "revoked", False)
        return {"account": "acct-1"}

    async def query_pending(command_id: str) -> dict[str, Any] | None:
        calls.append(f"query_pending:{command_id}")
        if fail_at == f"query_pending:{command_id}":
            raise TransportError("host_offline", "drop", True)
        return (statuses or {}).get(command_id)

    async def fetch_snapshot() -> dict[str, Any]:
        calls.append("fetch_snapshot")
        if fail_at == "fetch_snapshot":
            raise TransportError("host_offline", "drop", True)
        return {"cursor": "c9"}

    async def reconnect_cursor(cursor: str) -> None:
        calls.append(f"reconnect_cursor:{cursor}")
        if fail_at == "reconnect_cursor":
            raise TransportError("host_offline", "drop", True)

    async def reacquire_control() -> str:
        calls.append("reacquire_control")
        if fail_at == "reacquire_control":
            raise ShellOpError("lease_conflict", "taken over elsewhere")
        return "binding-2/gen-9"

    return ResumeOrchestrator(
        refresh_identity=refresh_identity,
        query_pending=query_pending,
        fetch_snapshot=fetch_snapshot,
        reconnect_cursor=reconnect_cursor,
        reacquire_control=reacquire_control,
    )


def test_resume_runs_des010_order_and_grants_control_last() -> None:
    async def scenario() -> None:
        calls: list[str] = []
        orchestrator = _orchestrator(calls, statuses={"cmd-1": {"status": "acked"}, "cmd-2": None})
        report = await orchestrator.resume(
            stored_route="/agents/sess-1",
            outstanding_command_ids=("cmd-1", "cmd-2"),
        )
        assert report.steps == RESUME_ORDER
        assert RESUME_ORDER == (
            "refresh_identity",
            "restore_route",
            "query_pending",
            "fetch_snapshot",
            "reconnect_cursor",
            "reacquire_control",
        )
        assert calls == [
            "refresh_identity",
            "query_pending:cmd-1",
            "query_pending:cmd-2",
            "fetch_snapshot",
            "reconnect_cursor:c9",
            "reacquire_control",
        ]
        assert report.route == RouteState(destination="agents", selection="sess-1")
        assert report.pending == {"cmd-1": "acked", "cmd-2": "unknown"}
        assert report.snapshot_cursor == "c9"
        assert report.control_granted is True
        assert isinstance(report, ResumeReport)

    asyncio.run(scenario())


def test_resume_aborts_at_each_step_without_granting_control() -> None:
    async def scenario() -> None:
        markers = (
            "refresh_identity",
            "restore_route",
            "query_pending",
            "fetch_snapshot",
            "reconnect_cursor",
            "reacquire_control",
        )
        for step in RESUME_ORDER:
            if step in ("restore_route", "query_pending"):
                continue  # covered separately below
            calls: list[str] = []
            orchestrator = _orchestrator(calls, fail_at=step)
            with pytest.raises(ResumeError) as failed:
                await orchestrator.resume(
                    stored_route="/agents/sess-1",
                    outstanding_command_ids=("cmd-1",),
                )
            assert failed.value.step == step
            # Steps after the failing one never ran; control is last, so a
            # grant is impossible on any failure path.
            failed_index = markers.index(step)
            for later in markers[failed_index + 1 :]:
                assert not any(c == later or c.startswith(later + ":") for c in calls), (
                    step,
                    later,
                    calls,
                )

    asyncio.run(scenario())


def test_resume_route_fallback_and_pending_unknown_never_resends() -> None:
    async def scenario() -> None:
        calls: list[str] = []
        # Unknown destination falls back instead of failing resume; a
        # failing status query marks THAT id unknown without aborting.
        orchestrator = _orchestrator(calls, fail_at="query_pending:cmd-x")
        report = await orchestrator.resume(
            stored_route="/nope/xyz",
            outstanding_command_ids=("cmd-x",),
        )
        assert report.route == RouteState()
        assert report.pending == {"cmd-x": "unknown"}
        assert report.control_granted is True
        # The orchestrator has no send path: only queries ran, one per id.
        queries = [c for c in calls if c.startswith("query_pending")]
        assert queries == ["query_pending:cmd-x"]

    asyncio.run(scenario())


def test_resume_rejects_snapshot_without_cursor() -> None:
    async def scenario() -> None:
        async def refresh_identity() -> dict[str, Any]:
            return {"account": "acct-1"}

        async def query_pending(command_id: str) -> dict[str, Any] | None:
            _ = command_id
            return None

        async def fetch_snapshot() -> dict[str, Any]:
            return {"cursor": ""}

        async def reconnect_cursor(cursor: str) -> None:
            _ = cursor
            raise AssertionError("must not reconnect without a cursor")

        async def reacquire_control() -> str:
            raise AssertionError("must not grant control without a cursor")

        orchestrator = ResumeOrchestrator(
            refresh_identity=refresh_identity,
            query_pending=query_pending,
            fetch_snapshot=fetch_snapshot,
            reconnect_cursor=reconnect_cursor,
            reacquire_control=reacquire_control,
        )
        with pytest.raises(ResumeError) as failed:
            await orchestrator.resume(stored_route="/agents")
        assert failed.value.step == "fetch_snapshot"

    asyncio.run(scenario())


def test_restore_minimal_route_carries_opaque_keys_only() -> None:
    assert restore_minimal_route("/terminals/term-9") == RouteState(
        destination="terminals", selection="term-9"
    )
    assert restore_minimal_route("/settings/ignored") == RouteState(
        destination="settings", selection=""
    )
    assert restore_minimal_route("garbage") == RouteState()


# -- process death: unknown, route-only, no resend ------------------------------


def test_process_death_drops_everything_restores_route_only() -> None:
    async def scenario() -> None:
        sent: list[bytes] = []

        async def sender(data: bytes) -> None:
            sent.append(data)

        gate = WriterGate()
        binding = gate.acquire(5, sender)
        memory = ProcessMemory(
            writer_bindings=[binding],
            outbox=[b"queued-1", b"queued-2"],
            unacked_input=["seq-11", "seq-12"],
            cursors={"h1": "c4"},
            snapshots={"h1": {"cursor": "c4"}},
            pending_commands=["cmd-7"],
        )
        durable = DurableStores(
            ordinary={"route": "/agents/sess-1", "theme": "dark"},
            secure={
                "access_token": "tok-live",
                "refresh_token": "tok-refresh",
                "draft:sess-1": "unsent draft text",
            },
        )
        report = simulate_death(memory, durable)
        gate.drop()  # the gate is in-memory state too
        assert isinstance(report, DeathReport)
        assert report.unknown_inputs == ("seq-11", "seq-12")
        assert report.dropped_outbox_chunks == 2
        assert report.route_restored == "/agents/sess-1"
        assert report.secrets_in_ordinary is False
        assert memory.is_empty() is True
        # Durable state survived: route + secure drafts/tokens.
        assert durable.ordinary == {"route": "/agents/sess-1", "theme": "dark"}
        assert durable.secure["draft:sess-1"] == "unsent draft text"
        # The dead writer can never emit; the dropped outbox is gone, so a
        # later resume cannot resend it (no double input across death).
        with pytest.raises(StaleWriterError) as dead:
            await gate.emit(binding, b"post-death")
        assert dead.value.reason == "dead"
        assert sent == []
        assert memory.outbox == []

    asyncio.run(scenario())


def test_process_death_flags_secrets_in_ordinary_prefs() -> None:
    memory = ProcessMemory(unacked_input=["seq-1"])
    durable = DurableStores(ordinary={"route": "/agents", "access_token": "tok-leak"})
    report = simulate_death(memory, durable)
    assert report.secrets_in_ordinary is True
    assert report.unknown_inputs == ("seq-1",)


def test_config_change_retains_everything_no_resend_no_dup() -> None:
    async def scenario() -> None:
        sent: list[bytes] = []

        async def sender(data: bytes) -> None:
            sent.append(data)

        machine = LifecycleMachine()
        gate = WriterGate()
        binding = gate.acquire(5, sender)
        memory = ProcessMemory(
            writer_bindings=[binding],
            outbox=[b"queued"],
            unacked_input=["seq-1"],
            cursors={"h1": "c4"},
        )
        await gate.emit(binding, b"before-rotate")
        assert sent == [b"before-rotate"]

        for kind in (CONFIG_ROTATE, CONFIG_KEYBOARD):
            change = machine.config_change(kind)
            retained = simulate_config_change(memory)
            assert retained is memory  # retained by identity
            assert machine.phase is AppPhase.FOREGROUND

        # No writer duplication (still the one binding), no resend (the
        # sent log is untouched), the writer still works.
        assert memory.writer_bindings == [binding]
        assert gate.is_live is True
        await gate.emit(binding, b"after-config")
        assert sent == [b"before-rotate", b"after-config"]
        assert change.kind in (CONFIG_ROTATE, CONFIG_KEYBOARD)

    asyncio.run(scenario())


# -- network switch: signal -> resync, dedupe ----------------------------------


def test_link_resync_records_events_and_requests_resync() -> None:
    port = FakeConnectivity()
    requested: list[ConnectivityState] = []
    supervisor = LinkResync(port, requested.append)
    assert supervisor.current is ConnectivityState.WIFI
    port.switch(ConnectivityState.MOBILE)
    port.switch(ConnectivityState.OFFLINE)
    port.switch(ConnectivityState.WIFI)
    assert [e.state for e in supervisor.events] == [
        ConnectivityState.MOBILE,
        ConnectivityState.OFFLINE,
        ConnectivityState.WIFI,
    ]
    assert [e.seq for e in supervisor.events] == [1, 2, 3]
    assert requested == [
        ConnectivityState.MOBILE,
        ConnectivityState.OFFLINE,
        ConnectivityState.WIFI,
    ]
    assert supervisor.current is ConnectivityState.WIFI


def test_cursor_deduper_drops_replays() -> None:
    deduper = CursorDeduper()
    first = deduper.filter([{"cursor": "c1", "sequence": 1}, {"cursor": "c2", "sequence": 2}])
    assert [e["sequence"] for e in first] == [1, 2]
    # A scripted switch mid-stream replays the overlap: duplicates die.
    replay = deduper.filter(
        [
            {"cursor": "c1", "sequence": 1},
            {"cursor": "c2", "sequence": 2},
            {"cursor": "c3", "sequence": 3},
        ]
    )
    assert [e["sequence"] for e in replay] == [3]
    assert deduper.duplicates == 2
    assert deduper.last_sequence == 3


def test_network_switch_mid_stream_resyncs_without_duplicates() -> None:
    async def scenario() -> None:
        transport = _LifecycleTransport()
        release = asyncio.Event()
        # First stream: one event, then park mid-stream (the Wi-Fi drop).
        transport.scripts.append([_meta(1), release, _meta(2)])
        # Resync replays from the snapshot cursor (sequence 1 again: the
        # snapshot resets last_sequence), then the switch duplicates one
        # frame mid-replay: the dup must die idempotently.
        transport.scripts.append([_meta(1), _meta(1), _meta(2), _meta(3)])
        controller = SyncController(transport=transport, backoff=Backoff.seeded(3), sleep=_no_sleep)
        task = asyncio.create_task(controller.run("h1", stop=lambda: transport.finished))
        try:
            await _wait_until(lambda: controller.state.connection.last_sequence == 1)
            assert controller.state.connection.state is ConnectionState.LIVE
            # The link signal fires mid-stream; the explicit resync uses
            # the existing snapshot + cursor-resume path.
            controller.request_resync()
            release.set()
            await _wait_until(lambda: transport.snapshots >= 2)
            await asyncio.wait_for(task, timeout=10)
        finally:
            if not task.done():
                task.cancel()
        assert transport.snapshots == 2
        assert len(transport.cursors) == 2  # snapshot + cursor resume, twice
        assert controller.state.connection.last_sequence == 3
        assert controller.state.connection.state is ConnectionState.LIVE
        # The ev-1 replay was an idempotent duplicate, not a second apply.
        assert controller.state.needs_resync is False

    asyncio.run(scenario())


def test_transport_drop_mid_stream_resyncs_without_duplicates() -> None:
    async def scenario() -> None:
        transport = _LifecycleTransport()
        transport.scripts.append([_meta(1), TransportError("host_offline", "drop", True)])
        transport.scripts.append([_meta(1), _meta(2)])
        controller = SyncController(transport=transport, backoff=Backoff.seeded(3), sleep=_no_sleep)
        await asyncio.wait_for(controller.run("h1", stop=lambda: transport.finished), timeout=10)
        assert transport.snapshots == 2
        assert controller.state.connection.last_sequence == 2
        assert controller.state.connection.state is ConnectionState.LIVE

    asyncio.run(scenario())


def test_link_signal_wired_to_controller_resync() -> None:
    async def scenario() -> None:
        transport = _LifecycleTransport()
        release = asyncio.Event()
        transport.scripts.append([_meta(1), release, _meta(2)])
        transport.scripts.append([_meta(1), _meta(2)])
        controller = SyncController(transport=transport, backoff=Backoff.seeded(3), sleep=_no_sleep)
        port = FakeConnectivity()
        supervisor = LinkResync(port, lambda state: controller.request_resync())
        task = asyncio.create_task(controller.run("h1", stop=lambda: transport.finished))
        try:
            await _wait_until(lambda: controller.state.connection.last_sequence == 1)
            port.switch(ConnectivityState.MOBILE)  # Wi-Fi -> mobile mid-stream
            release.set()
            await _wait_until(lambda: transport.snapshots >= 2)
            await asyncio.wait_for(task, timeout=10)
        finally:
            if not task.done():
                task.cancel()
        assert [e.state for e in supervisor.events] == [ConnectivityState.MOBILE]
        assert transport.snapshots == 2
        assert controller.state.connection.last_sequence == 2

    asyncio.run(scenario())


def test_link_signal_recycles_with_no_further_frames() -> None:
    async def scenario() -> None:
        transport = _LifecycleTransport()
        release = asyncio.Event()  # NEVER set: the half-open park
        transport.scripts.append([_meta(1), release])
        transport.scripts.append([_meta(1), _meta(2)])
        controller = SyncController(transport=transport, backoff=Backoff.seeded(3), sleep=_no_sleep)
        task = asyncio.create_task(controller.run("h1", stop=lambda: transport.finished))
        try:
            await _wait_until(lambda: controller.state.connection.last_sequence == 1)
            controller.request_resync()
            # No further frames arrive, yet the parked pull recycles:
            # the stream is closed and snapshot + cursor resume run.
            await _wait_until(lambda: transport.snapshots >= 2)
            await asyncio.wait_for(task, timeout=10)
        finally:
            if not task.done():
                task.cancel()
        assert transport.snapshots == 2
        assert len(transport.cursors) == 2  # snapshot + cursor resume, twice
        assert controller.state.connection.last_sequence == 2
        assert controller.state.connection.state is ConnectionState.LIVE

    asyncio.run(scenario())


def test_rapid_link_signals_coalesce_into_single_recovery() -> None:
    async def scenario() -> None:
        transport = _LifecycleTransport()
        release = asyncio.Event()
        transport.scripts.append([_meta(1), release, _meta(2)])
        transport.scripts.append([_meta(1), _meta(2)])
        controller = SyncController(transport=transport, backoff=Backoff.seeded(3), sleep=_no_sleep)
        task = asyncio.create_task(controller.run("h1", stop=lambda: transport.finished))
        try:
            await _wait_until(lambda: controller.state.connection.last_sequence == 1)
            # Flap: three rapid signals while parked mid-stream.
            controller.request_resync()
            controller.request_resync()
            controller.request_resync()
            await _wait_until(lambda: transport.snapshots >= 2)
            release.set()  # the parked event firing late changes nothing now
            await asyncio.wait_for(task, timeout=10)
        finally:
            if not task.done():
                task.cancel()
        # Serialized single recovery: exactly one resync, no pile-up.
        assert transport.snapshots == 2
        assert len(transport.cursors) == 2
        assert controller.state.connection.last_sequence == 2
        assert controller.state.connection.state is ConnectionState.LIVE

    asyncio.run(scenario())


def test_link_resync_history_is_bounded() -> None:
    port = FakeConnectivity()
    delivered: list[ConnectivityState] = []
    supervisor = LinkResync(port, delivered.append)
    states = (ConnectivityState.MOBILE, ConnectivityState.WIFI, ConnectivityState.OFFLINE)
    for i in range(LINK_EVENT_HISTORY + 8):
        port.switch(states[i % 3])
    # Every signal is still delivered; only the retained history is bounded
    # (monotonic seqs, last LINK_EVENT_HISTORY kept).
    assert len(delivered) == LINK_EVENT_HISTORY + 8
    assert len(supervisor.events) == LINK_EVENT_HISTORY
    assert [e.seq for e in supervisor.events] == list(range(9, LINK_EVENT_HISTORY + 9))


# -- home independence: FakeBackend over production lease/ring stores -------------
#
# Proven against FakeBackend, which delegates lease/ring/expiry to the
# PRODUCTION LeaseStore/RingStore (the same classes the gateway serves) --
# this is NOT live-gateway proof. Remote-process aliveness is proxied by
# `attachment.closed`; real home-Mac processes are unverifiable headless.


def test_home_independence_client_death_keeps_server_alive() -> None:
    now = [1_000_000]
    backend = FakeBackend(now_ms=lambda: now[0])
    attachment, displaced = backend.create_attachment(
        "home-mac",
        "term-1",
        "shell",
        "acct-1",
        "dev-1",
        takeover=False,
        owner_label="MBP",
    )
    assert displaced == ()
    lease = backend.acquire_control(attachment.attachment_id, "acct-1", "dev-1")
    assert backend.publish_output(attachment.attachment_id, attachment.epoch, b"hello ") == (0, 6)
    assert backend.publish_output(attachment.attachment_id, attachment.epoch, b"world") == (6, 11)

    # Kill the client mid-stream: every client-held handle is dropped. The
    # harness keeps only what the SERVER knows (ids are server-side facts).
    # "Process alive" below is proxied by `attachment.closed` on FakeBackend
    # (production lease/ring stores); no home-Mac process is observed here.
    server_lease_id = lease.lease_id
    server_epoch = attachment.epoch
    server_attachment_id = attachment.attachment_id
    del lease

    live = backend.get_attachment(server_attachment_id)
    assert live is not None and live.closed is False
    assert (live.generation, live.epoch, live.terminal_id) == (1, server_epoch, "term-1")
    batch = backend.read_stream(server_lease_id, server_epoch, 0)
    assert batch.status == STATUS_OK
    assert b"".join(chunk for _, chunk in batch.chunks) == b"hello world"

    # Release detaches the lease; the attachment, ring, and remote process
    # survive (release/expiry paths unchanged).
    released = backend.release_lease(server_lease_id, "acct-1", "dev-1")
    assert released.closed is False
    assert released.generation == 1
    assert backend.ownership.open_lease(server_lease_id, now[0]) is None
    view = backend.issue_view(server_attachment_id, "acct-1", "dev-1")
    reread = backend.read_stream(view.lease_id, server_epoch, 0)
    assert reread.status == STATUS_OK
    assert b"".join(chunk for _, chunk in reread.chunks) == b"hello world"

    # Renew extends a live lease; expiry kills only the lease, never the
    # attachment or its ring.
    control = backend.acquire_control(server_attachment_id, "acct-1", "dev-1")
    renewed = backend.renew_lease(control.lease_id, "acct-1", "dev-1")
    assert renewed.expires_ms == now[0] + CONTROL_LEASE_TTL_MS
    now[0] += CONTROL_LEASE_TTL_MS + 1
    assert backend.ownership.open_lease(control.lease_id, now[0]) is None
    after_expiry = backend.get_attachment(server_attachment_id)
    assert after_expiry is not None and after_expiry.closed is False

    # Only explicit close ends the remote pane.
    dead = backend.close_attachment(server_attachment_id, "acct-1")
    assert view.lease_id in dead
    closed = backend.get_attachment(server_attachment_id)
    assert closed is not None and closed.closed is True


# -- storage policy + force-stop limits -----------------------------------------


def test_storage_policy_text_is_exact() -> None:
    assert "SECURE storage only" in STORAGE_POLICY
    assert "ordinary preferences hold only" in STORAGE_POLICY
    assert "re-validated after login" in STORAGE_POLICY
    assert "never downgrades to plaintext" in STORAGE_POLICY


def test_audit_stores_flags_violations() -> None:
    clean = DurableStores(
        ordinary={"route": "/agents/sess-1", "theme": "dark"},
        secure={"access_token": "tok", "draft:sess-1": "draft"},
    )
    assert audit_stores(clean.ordinary, clean.secure) == []

    dirty = DurableStores(
        ordinary={
            "route": "/agents",
            "access_token": "tok-leak",
            "draft:sess-1": "draft-leak",
            "mystery": "unknown-key",
        },
        secure={},
    )
    violations = audit_stores(dirty.ordinary, dirty.secure)
    assert any("secure-only key 'access_token'" in v for v in violations)
    assert any("secure-only key 'draft:sess-1'" in v for v in violations)
    assert any("unknown key 'mystery'" in v for v in violations)

    # A token stored under an innocent key is still caught by value scan.
    leak = audit_stores({"route": "/agents tok-secret-1"}, {}, known_secrets=("tok-secret-1",))
    assert any("leak a secret value in 'route'" in v for v in leak)


def test_force_stop_limits_documented() -> None:
    assert "NOTHING works until the user manually relaunches" in FORCE_STOP_LIMITS
    assert "no FCM delivery" in FORCE_STOP_LIMITS
    assert "cannot start a foreground service from the background" in FORCE_STOP_LIMITS
    assert "reboot" in FORCE_STOP_LIMITS
    assert "Doze" in FORCE_STOP_LIMITS
    assert "best-effort" in FORCE_STOP_LIMITS
    assert "source of truth" in FORCE_STOP_LIMITS


# -- sessions: pause/resume wiring ------------------------------------------------


class _MintStolenOps(ScriptedShellGatewayOps):
    """Ticket mint always fails: the lease was taken over elsewhere."""

    async def terminal_ticket(self, lease_id: str) -> str:
        self.tickets.append(lease_id)
        raise ShellOpError("lease_expired", "lease taken over elsewhere")


class _ReleaseFailOps(ScriptedShellGatewayOps):
    """Lease release always fails (dropped on the floor)."""

    async def release_lease(self, lease_id: str) -> None:
        self.releases.append(lease_id)
        raise ShellOpError("host_offline", "release dropped on the floor")


def test_shell_pause_invalidates_writer_and_resume_reacquires() -> None:
    async def scenario() -> None:
        session, ops, factory = _shell()
        assert await session.open("term-1") == SHELL_LIVE
        assert session.keys_enabled is True
        assert await session.type_text("ls") == "pty"
        live_socket = factory.sockets[0]
        assert len(live_socket.sent_bytes) == 1
        old_controller = session.controller

        assert await session.pause() == SHELL_DISCONNECTED
        assert session.paused is True
        assert session.lease is None
        assert ops.releases != []
        assert old_controller is not None and old_controller.writer_paused is True
        assert session.keys_enabled is False

        # Paused input is dropped, never queued: no stale writer, no resend.
        assert await session.type_text("x") == "dropped"
        assert await session.press_key("a") is False
        await session.request_paste("one line")
        assert len(live_socket.sent_bytes) == 1

        assert await session.resume_after_pause() == SHELL_LIVE
        assert session.paused is False
        assert len(factory.sockets) == 2  # exactly one fresh writer, no dup
        assert len(live_socket.sent_bytes) == 1  # old socket got no resend
        assert session.controller is not old_controller
        assert session.controller is not None
        assert session.controller.writer_paused is False
        # Re-owning our own released lease self-takeovers (not a steal):
        # the last attach took over after our own conflict.
        assert ops.attaches[-1]["takeover"] is True
        assert await session.type_text("pwd") == "pty"
        assert len(factory.sockets[1].sent_bytes) == 1

    asyncio.run(scenario())


def test_shell_resume_after_takeover_elsewhere_discloses_no_steal() -> None:
    async def scenario() -> None:
        session, ops, factory = _shell()
        assert await session.open("term-1") == SHELL_LIVE
        assert await session.pause() == SHELL_DISCONNECTED
        takes_before = [a for a in ops.attaches if a["takeover"]]
        # Another device owns the terminal while we are paused.
        ops.seed_owner("home-mac", "term-1", "dev-other", "Other Phone")
        assert await session.resume_after_pause() == "conflict"
        assert session.paused is False
        assert session.owner_label == "Other Phone"
        # Resume never silently steals: no new takeover attach ran.
        assert [a for a in ops.attaches if a["takeover"]] == takes_before
        assert len(factory.sockets) == 1

    asyncio.run(scenario())


def test_shell_pause_on_exited_is_noop() -> None:
    async def scenario() -> None:
        session, _, _ = _shell()
        assert await session.open("term-1") == SHELL_LIVE
        await session.close_terminal()
        assert await session.pause() == SHELL_EXITED
        assert session.paused is False
        assert await session.resume_after_pause() == SHELL_EXITED

    asyncio.run(scenario())


def test_taken_over_lease_fails_ticket_mint_as_disconnected() -> None:
    async def scenario() -> None:
        ops = _MintStolenOps()
        factory = _SocketFactory()
        session = ShellSession(
            ops=ops,
            host_id="home-mac",
            device_id=ops.device_id,
            socket_factory=factory,
            clipboard=FakeClipboard(),
        )
        # The REAL session path maps a mint failure to disconnected
        # ("Could not stream"), never to conflict: the lease we hold is
        # dead, not contended.
        assert await session.open("term-1") == SHELL_DISCONNECTED
        assert session.phase != SHELL_CONFLICT
        assert session.owner_label == ""
        assert session.notice == "Could not stream (lease_expired)."
        assert factory.sockets == []  # the stream never started
        assert session.keys_enabled is False

        # Same mapping on the explicit-takeover mint site.
        assert await session.take_over() == SHELL_DISCONNECTED
        assert session.phase != SHELL_CONFLICT
        assert session.owner_label == ""
        assert factory.sockets == []

    asyncio.run(scenario())


def test_queued_keys_die_across_pause_and_retarget_clears() -> None:
    async def scenario() -> None:
        # Queued input cannot flush across pause: the probe is dead, so
        # the flush drops the queue with zero delivery.
        session, _, factory = _shell()
        assert await session.open("term-1") == SHELL_LIVE
        live_socket = factory.sockets[0]
        assert len(live_socket.sent_bytes) == 0
        assert session.sessions.keys.press_key("a") is not None
        assert len(session.sessions.keys.outbox) == 1
        assert await session.pause() == SHELL_DISCONNECTED
        assert session.sessions.keys.press_key("b") is None  # cannot queue while paused
        assert await session.sessions.keys.flush() == 0
        assert len(live_socket.sent_bytes) == 0
        assert len(session.sessions.keys.outbox) == 0

        # Queued input cannot survive resume-retarget either: a queue
        # left in place at pause is dropped by the retarget, never
        # replayed onto the fresh writer.
        session, _, factory = _shell()
        assert await session.open("term-1") == SHELL_LIVE
        live_socket = factory.sockets[0]
        assert session.sessions.keys.press_key("a") is not None
        assert await session.pause() == SHELL_DISCONNECTED
        assert len(session.sessions.keys.outbox) == 1  # still queued, undelivered
        assert await session.resume_after_pause() == SHELL_LIVE
        assert len(session.sessions.keys.outbox) == 0
        assert await session.sessions.keys.flush() == 0
        assert len(live_socket.sent_bytes) == 0  # old socket: no resend
        assert len(factory.sockets) == 2
        assert len(factory.sockets[1].sent_bytes) == 0  # fresh socket: no replay

    asyncio.run(scenario())


def test_open_review_across_pause_is_stale_nothing_sent() -> None:
    async def scenario() -> None:
        session, _, factory = _shell()
        assert await session.open("term-1") == SHELL_LIVE
        live_socket = factory.sockets[0]
        assert await session.request_paste("line1\nline2") == REQUEST_REVIEW
        assert session.pending_review is not None
        assert await session.pause() == SHELL_DISCONNECTED
        # Confirm after pause is impossible: the bound generation is
        # gone, so the review goes stale and nothing is sent.
        assert await session.confirm_paste() is False
        assert session.pending_review is None
        assert session.paste_error == STALE_MESSAGE
        assert len(live_socket.sent_bytes) == 0
        # Resume binds a fresh paste session: no review, no replay.
        assert await session.resume_after_pause() == SHELL_LIVE
        assert session.pending_review is None
        assert len(factory.sockets[1].sent_bytes) == 0

    asyncio.run(scenario())


def test_failing_release_still_invalidates_writer() -> None:
    async def scenario() -> None:
        ops = _ReleaseFailOps()
        factory = _SocketFactory()
        session = ShellSession(
            ops=ops,
            host_id="home-mac",
            device_id=ops.device_id,
            socket_factory=factory,
            clipboard=FakeClipboard(),
        )
        assert await session.open("term-1") == SHELL_LIVE
        live_socket = factory.sockets[0]
        old_controller = session.controller
        assert old_controller is not None

        # The release throws, but pause still retires the local writer:
        # invalidation runs before the best-effort release.
        assert await session.pause() == SHELL_DISCONNECTED
        assert session.lease is None
        assert old_controller.writer_paused is True
        assert "Pause noted (host_offline)" in session.notice

        # Every emit fails; the dead socket gets nothing more.
        assert await session.type_text("x") == "dropped"
        assert await session.press_key("a") is False
        with pytest.raises(PasteError) as paste:
            await old_controller.send_paste_chunk(b"x")
        assert paste.value.code == "no_writer"
        with pytest.raises(KeyInputError) as key:
            await old_controller.send_key_bytes(b"y")
        assert key.value.code == "no_writer"
        assert len(live_socket.sent_bytes) == 0

    asyncio.run(scenario())


def test_modifiers_and_ime_survive_pause_cleared_on_retarget() -> None:
    async def scenario() -> None:
        session, _, factory = _shell()
        assert await session.open("term-1") == SHELL_LIVE
        session.toggle_modifier("ctrl")
        session.sessions.ime.begin()
        assert session.sessions.keys.pending_modifiers == frozenset({"ctrl"})
        assert session.sessions.ime.is_composing is True

        # Pause preserves both (nothing lost) but neither can emit: the
        # writer is dead, and the armed set survives the failed press.
        assert await session.pause() == SHELL_DISCONNECTED
        assert session.sessions.keys.pending_modifiers == frozenset({"ctrl"})
        assert session.sessions.ime.is_composing is True
        assert session.sessions.keys.press_key("c") is None
        assert session.sessions.keys.pending_modifiers == frozenset({"ctrl"})
        assert await session.sessions.keys.flush() == 0

        # Resume-retarget clears both AFTER the no-emit window: the safe
        # ordering is preserve-then-clear, never emit-then-clear.
        assert await session.resume_after_pause() == SHELL_LIVE
        assert session.sessions.keys.pending_modifiers == frozenset()
        assert session.sessions.ime.is_composing is False
        fresh = session.sessions.keys.press_key("c")
        assert fresh is not None and fresh.data == b"c"  # plain c: ctrl never leaked
        assert await session.sessions.keys.flush() == 1
        assert len(factory.sockets[1].sent_bytes) == 1

    asyncio.run(scenario())


def test_agent_pause_drops_review_keeps_draft_resume_needs_confirm() -> None:
    async def scenario() -> None:
        session, ops, factory, _ = _agent(initial_status="blocked")
        session.open()
        assert await session.confirm_takeover() == AGENT_LIVE
        session.on_draft_change("line1\nline2")
        assert await session.on_send() == SEND_BLOCKED_REVIEW
        assert session.store.pending_blocked is not None

        snapshot = session.store.export_draft()
        durable = DurableStores(ordinary={"route": "/agents/agent-1"}, secure={})
        durable.secure["draft:agent-1"] = json.dumps(snapshot)

        assert await session.pause() == AGENT_DISCONNECTED
        assert session.paused is True
        assert session.lease is None
        # In-flight review dropped, draft preserved byte-identical, nothing sent.
        assert session.store.pending_blocked is None
        assert session.store.draft == "line1\nline2"
        assert session.store.error == REVIEW_LOST_MESSAGE
        assert audit_stores(durable.ordinary, durable.secure) == []

        attaches_before = list(ops.attaches)
        assert await session.resume_after_pause() == AGENT_DISCONNECTED
        assert session.paused is False
        # Reconnect re-arms the disclosure and performs NO attach: resume
        # cannot silently steal (agent attach always takes ownership).
        assert session.takeover_disclosure is not None
        assert ops.attaches == attaches_before
        assert len(factory.sockets) == 1
        assert await session.confirm_takeover() == AGENT_LIVE
        session.store.import_draft(json.loads(durable.secure["draft:agent-1"]))
        assert session.store.draft == "line1\nline2"

    asyncio.run(scenario())


def test_agent_pause_marks_inflight_send_unknown_never_replays() -> None:
    async def scenario() -> None:
        ops = ScriptedShellGatewayOps()
        factory = _SocketFactory()
        gate = asyncio.Event()

        async def submit(command: dict[str, Any]) -> dict[str, Any]:
            _ = command
            await gate.wait()
            return {"status": "accepted"}

        async def prompt(target: str, text: str) -> dict[str, Any]:
            _ = (target, text)
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
            clipboard=FakeClipboard(),
        )
        session.open()
        assert await session.confirm_takeover() == AGENT_LIVE
        session.on_draft_change("hello agent")
        task = asyncio.create_task(session.on_send())
        await _wait_until(lambda: session.store.submission == SUBMISSION_SENDING)
        assert await session.pause() == AGENT_DISCONNECTED
        assert session.store.submission == SUBMISSION_UNKNOWN
        assert session.store.error == LOST_DELIVERY_MESSAGE
        assert session.store.draft == "hello agent"  # draft preserved for manual resend
        gate.set()
        # The late outcome is discarded by the send generation: unknown wins.
        assert await task == "ignored"
        assert session.store.submission == SUBMISSION_UNKNOWN

    asyncio.run(scenario())


def test_agent_pause_on_exited_is_noop() -> None:
    async def scenario() -> None:
        session, _, factory, _ = _agent()
        session.open()
        # Queue the exit signal before the takeover spawns the socket
        # (the agent surface has no explicit close; the reserved bye
        # signal is its proven exit path).
        factory.queue(("text", json.dumps({"kind": "bye", "detail": "closed"})))
        assert await session.confirm_takeover() == AGENT_LIVE
        assert await session.run() == AGENT_EXITED
        assert await session.pause() == AGENT_EXITED
        assert session.paused is False
        assert await session.resume_after_pause() == AGENT_EXITED

    asyncio.run(scenario())


def test_pause_persist_roundtrip_secure_store() -> None:
    async def scenario() -> None:
        store = FakeCredentialStore()
        snapshot = {
            "draft": "run tok-1 now",
            "markers": [{"token": "tok-1", "resolved": False}],
        }
        report = await pause_persist(store, draft_key("agent-1"), snapshot)
        assert report.key == "draft-agent-1"
        assert report.persisted is True
        assert report.held_snapshot is None
        assert report.capability_error is None

        found = await resume_restore(store, draft_key("agent-1"))
        assert found.found is True
        assert found.snapshot == snapshot
        assert found.capability_error is None

        missing = await resume_restore(store, draft_key("agent-9"))
        assert missing.found is False
        assert missing.snapshot is None
        assert missing.capability_error is None

        await store.set(draft_key("agent-2"), "not-json{{{")
        corrupt = await resume_restore(store, draft_key("agent-2"))
        assert corrupt.found is False
        assert corrupt.snapshot is None
        assert corrupt.capability_error == "stored draft snapshot is corrupt"

    asyncio.run(scenario())


def test_pause_persist_outage_holds_in_memory_never_plaintext() -> None:
    async def scenario() -> None:
        store = FakeCredentialStore(available=False)
        ordinary: dict[str, str] = {}  # plaintext prefs the helper must never touch
        snapshot = {"draft": "secret work", "markers": []}
        report = await pause_persist(store, draft_key("agent-1"), snapshot)
        assert report.persisted is False
        assert report.held_snapshot == snapshot
        assert report.capability_error is not None
        assert "memory only" in report.capability_error
        assert "never plaintext" in report.capability_error
        # Zero writes anywhere durable: secure is empty, plaintext untouched.
        assert await store.get(draft_key("agent-1")) is None
        assert ordinary == {}
        assert audit_stores(ordinary, {}) == []

        restore = await resume_restore(store, draft_key("agent-1"))
        assert restore.found is False
        assert restore.snapshot is None
        assert restore.capability_error == OUTAGE_MESSAGE

    asyncio.run(scenario())


def test_agent_pause_persists_and_resume_restores_draft() -> None:
    async def scenario() -> None:
        store = FakeCredentialStore()
        session, _, _, _ = _agent(draft_store=store)
        session.open()
        assert await session.confirm_takeover() == AGENT_LIVE
        session.on_draft_change("unsent work")
        session.store.stage_marker("tok-1")
        assert await session.pause() == AGENT_DISCONNECTED

        persist = session.last_pause_persist
        assert persist is not None
        assert persist.persisted is True
        assert persist.capability_error is None
        assert json.loads(await store.get("draft-home-mac:agent-1") or "") == {
            "draft": "unsent work",
            "markers": [{"token": "tok-1", "resolved": False}],
        }

        # Prove the restore reads durable state, not memory: clobber the
        # live draft, then resume and watch the persisted copy win.
        session.on_draft_change("clobbered after pause")
        assert await session.resume_after_pause() == AGENT_DISCONNECTED
        restore = session.last_resume_restore
        assert restore is not None and restore.found is True
        assert session.store.draft == "unsent work"
        assert [m.token for m in session.store.markers] == ["tok-1"]

    asyncio.run(scenario())


def test_agent_pause_secure_outage_surfaces_capability_error() -> None:
    async def scenario() -> None:
        store = FakeCredentialStore(available=False)
        session, _, _, _ = _agent(draft_store=store)
        session.open()
        assert await session.confirm_takeover() == AGENT_LIVE
        session.on_draft_change("held work")
        assert await session.pause() == AGENT_DISCONNECTED

        persist = session.last_pause_persist
        assert persist is not None
        assert persist.persisted is False
        assert persist.held_snapshot == {"draft": "held work", "markers": []}
        assert persist.capability_error is not None
        assert "memory only" in session.notice
        # The live draft is intact and nothing reached durable storage.
        assert session.store.draft == "held work"
        assert await store.get("draft-home-mac:agent-1") is None

        # Resume on outage keeps the live draft (no durable copy to win).
        session.on_draft_change("held work edited")
        assert await session.resume_after_pause() == AGENT_DISCONNECTED
        assert session.store.draft == "held work edited"

    asyncio.run(scenario())


def test_composer_draft_export_import_roundtrip() -> None:
    async def submit(command: dict[str, Any]) -> dict[str, Any]:
        _ = command
        return {"status": "accepted"}

    async def prompt(target: str, text: str) -> dict[str, Any]:
        _ = (target, text)
        return {"ok": True}

    def make() -> ComposerStore:
        return ComposerStore(
            target="sess:1",
            submit_command=submit,
            prompt_text=prompt,
            host_id="home-mac",
            host_epoch="epoch-1",
            agent_id="agent-1",
        )

    store = make()
    store.replace_draft("run tok-1 now")
    store.stage_marker("tok-1")
    snapshot = store.export_draft()
    assert snapshot == {
        "draft": "run tok-1 now",
        "markers": [{"token": "tok-1", "resolved": False}],
    }
    clone = make()
    clone.import_draft(json.loads(json.dumps(snapshot)))
    assert clone.draft == "run tok-1 now"
    assert [m.token for m in clone.markers] == ["tok-1"]
    assert clone.can_send is False  # unresolved marker still blocks Send
    with pytest.raises(ValueError):
        clone.import_draft({"draft": 42, "markers": []})
    with pytest.raises(ValueError):
        clone.import_draft({"draft": "x", "markers": [{"token": "t"}]})
    assert clone.draft == "run tok-1 now"  # nothing half-applied
