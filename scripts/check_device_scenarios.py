"""Headless lifecycle scenario checks.

Runs the harness-executable lifecycle checks (pause-invalidates-writer,
ordered resume, process-death recovery, config-change survival,
network-switch resync, storage audit, home independence against
FakeBackend over the production lease/ring stores -- NOT a live gateway:
remote-process aliveness is proxied by `attachment.closed`, and real
home-Mac processes are unverifiable headless) and prints the
precisely-blocked real-device matrix: every hardware scenario
this environment cannot prove, with its exact missing environment per
item.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
from typing import Any

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from relaydeck.client.adapters.fake import FakeConnectivity  # noqa: E402
from relaydeck.client.adapters.ports import ConnectivityState  # noqa: E402
from relaydeck.client.features.lifecycle import (  # noqa: E402
    FORCE_STOP_LIMITS,
    PAUSE_RULE,
    RESUME_ORDER,
    STORAGE_POLICY,
    AppPhase,
    CursorDeduper,
    DurableStores,
    LifecycleMachine,
    LinkResync,
    ProcessMemory,
    ResumeOrchestrator,
    StaleWriterError,
    WriterGate,
    audit_stores,
    simulate_config_change,
    simulate_death,
)
from relaydeck.client.features.lifecycle.machine import (  # noqa: E402
    CONFIG_KEYBOARD,
    CONFIG_ROTATE,
)
from relaydeck.client.features.sync.transport import TerminalSocket  # noqa: E402
from relaydeck.client.features.terminal.controller import TerminalController  # noqa: E402
from relaydeck.client.features.terminal.keys import KeyInputError  # noqa: E402
from relaydeck.client.features.terminal.paste_policy import PasteError  # noqa: E402
from relaydeck.gateway.backend import FakeBackend  # noqa: E402
from relaydeck.gateway.ring import STATUS_OK  # noqa: E402

FAILURES: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    status = "PASS" if cond else "FAIL"
    print(f"[{status}] {name}" + (f": {detail}" if detail and not cond else ""))
    if not cond:
        FAILURES.append(name)


def _noop(data: bytes) -> None:
    _ = data


class _Socket(TerminalSocket):
    def __init__(self) -> None:
        self.sent_bytes: list[bytes] = []
        self.connected = False

    async def connect(self) -> None:
        self.connected = True

    async def send_bytes(self, data: bytes) -> None:
        self.sent_bytes.append(data)

    async def send_text(self, data: str) -> None:
        _ = data

    async def receive(self) -> tuple[str, Any]:
        raise AssertionError("no inbound frames scripted")

    async def close(self) -> None:
        self.connected = False


def check_pause_rule() -> None:
    async def scenario() -> None:
        sent: list[bytes] = []

        async def sender(data: bytes) -> None:
            sent.append(data)

        gate = WriterGate()
        binding = gate.acquire(7, sender)
        await gate.emit(binding, b"live")
        gate.pause()
        try:
            await gate.emit(binding, b"paused")
            check("pause: emit-after-pause fails", False)
        except StaleWriterError as exc:
            check("pause: emit-after-pause fails", exc.reason == "paused")
        check("pause: nothing delivered while paused", sent == [b"live"])
        fresh = gate.acquire(7, sender)
        try:
            await gate.emit(binding, b"stale")
            check("pause: retired binding never emits again", False)
        except StaleWriterError:
            check("pause: retired binding never emits again", True)
        await gate.emit(fresh, b"resumed")
        check("pause: explicit re-acquire works", sent[-1] == b"resumed")

        sock = _Socket()
        controller = TerminalController(
            socket_factory=lambda ticket: sock,
            lease_id="lease-1",
            generation=4,
            mode="control",
            on_bytes=_noop,
        )
        await controller.start("ticket-1", "", 0)
        controller.pause_writer()
        check("pause: controller probe silent while paused", controller.paste_generation() is None)
        try:
            await controller.send_paste_chunk(b"x")
            check("pause: controller paste fails closed", False)
        except PasteError as exc:
            check("pause: controller paste fails closed", exc.code == "no_writer")
        try:
            await controller.send_key_bytes(b"y")
            check("pause: controller keys fail closed", False)
        except KeyInputError as exc:
            check("pause: controller keys fail closed", exc.code == "no_writer")

    asyncio.run(scenario())
    check("pause: rule text present", "PAUSE invalidates the control writer" in PAUSE_RULE)


def check_resume_order() -> None:
    async def scenario() -> None:
        calls: list[str] = []

        async def refresh_identity() -> dict[str, Any]:
            calls.append("refresh_identity")
            return {"account": "acct-1"}

        async def query_pending(command_id: str) -> dict[str, Any] | None:
            calls.append(f"query_pending:{command_id}")
            return None

        async def fetch_snapshot() -> dict[str, Any]:
            calls.append("fetch_snapshot")
            return {"cursor": "c9"}

        async def reconnect_cursor(cursor: str) -> None:
            calls.append(f"reconnect_cursor:{cursor}")

        async def reacquire_control() -> str:
            calls.append("reacquire_control")
            return "binding-2"

        orchestrator = ResumeOrchestrator(
            refresh_identity=refresh_identity,
            query_pending=query_pending,
            fetch_snapshot=fetch_snapshot,
            reconnect_cursor=reconnect_cursor,
            reacquire_control=reacquire_control,
        )
        report = await orchestrator.resume(
            stored_route="/agents/sess-1", outstanding_command_ids=("cmd-1",)
        )
        check("resume: six steps in the documented order", report.steps == RESUME_ORDER)
        check("resume: control granted last", calls[-1] == "reacquire_control")
        check("resume: unknown pending stays unknown", report.pending == {"cmd-1": "unknown"})
        check("resume: current state before control", report.snapshot_cursor == "c9")

    asyncio.run(scenario())


def check_death_and_config() -> None:
    machine = LifecycleMachine()
    machine.on_background()
    machine.apply_pause_policy()
    memory = ProcessMemory(
        writer_bindings=[1], outbox=[b"q"], unacked_input=["seq-1"], cursors={"h1": "c4"}
    )
    retained = simulate_config_change(memory)
    check("config: rotate retains holders", retained is memory)
    rotated = machine.config_change(CONFIG_ROTATE)
    check("config: rotate is not a transition", rotated.phase is machine.phase)
    check(
        "config: keyboard-open is not a transition",
        machine.config_change(CONFIG_KEYBOARD).phase is machine.phase,
    )
    check("config: phase unchanged", machine.phase is AppPhase.PAUSED)

    durable = DurableStores(
        ordinary={"route": "/agents/sess-1"},
        secure={"access_token": "tok", "draft:sess-1": "draft"},
    )
    report = simulate_death(memory, durable)
    check("death: unacked becomes unknown", report.unknown_inputs == ("seq-1",))
    check("death: outbox dropped without resend", report.dropped_outbox_chunks == 1)
    check("death: route-only restore", report.route_restored == "/agents/sess-1")
    check("death: memory empty", memory.is_empty())
    check("death: no secrets in ordinary prefs", report.secrets_in_ordinary is False)


def check_network_dedupe() -> None:
    deduper = CursorDeduper()
    deduper.filter([{"cursor": "c1", "sequence": 1}, {"cursor": "c2", "sequence": 2}])
    replay = deduper.filter(
        [
            {"cursor": "c1", "sequence": 1},
            {"cursor": "c2", "sequence": 2},
            {"cursor": "c3", "sequence": 3},
        ]
    )
    check("network: switch replay deduped", [e["sequence"] for e in replay] == [3])
    check("network: duplicates counted", deduper.duplicates == 2)

    port = FakeConnectivity()
    requested: list[ConnectivityState] = []
    supervisor = LinkResync(port, requested.append)
    port.switch(ConnectivityState.MOBILE)
    seen = [e.state for e in supervisor.events]
    check("network: link signal recorded", seen == [ConnectivityState.MOBILE])
    check("network: resync requested on switch", requested == [ConnectivityState.MOBILE])


def check_storage_policy() -> None:
    pinned = "drafts live in SECURE storage" in STORAGE_POLICY
    check("storage: policy pins secure-only drafts", pinned)
    clean = DurableStores(ordinary={"route": "/agents"}, secure={"access_token": "t"})
    check("storage: clean audit passes", audit_stores(clean.ordinary, clean.secure) == [])
    dirty = DurableStores(ordinary={"access_token": "leak"}, secure={})
    check("storage: token in prefs flagged", audit_stores(dirty.ordinary, dirty.secure) != [])


def check_home_independence() -> None:
    now = [1_000_000]
    backend = FakeBackend(now_ms=lambda: now[0])
    attachment, _ = backend.create_attachment(
        "home-mac", "term-1", "shell", "acct-1", "dev-1", takeover=False
    )
    lease = backend.acquire_control(attachment.attachment_id, "acct-1", "dev-1")
    backend.publish_output(attachment.attachment_id, attachment.epoch, b"mid-stream")
    lease_id = lease.lease_id  # server-side id; the client handles are dropped
    del lease
    live = backend.get_attachment(attachment.attachment_id)
    # `closed is False` proxies "remote process alive" on FakeBackend
    # (production lease/ring stores); no home-Mac process is observed here.
    check("home: attachment survives client death", live is not None and live.closed is False)
    batch = backend.read_stream(lease_id, attachment.epoch, 0)
    check(
        "home: ring bytes survive client death",
        batch.status == STATUS_OK and b"".join(chunk for _, chunk in batch.chunks) == b"mid-stream",
    )
    released = backend.release_lease(lease_id, "acct-1", "dev-1")
    check("home: release keeps attachment alive", released.closed is False)


BLOCKED_MATRIX: list[tuple[str, str]] = [
    (
        "rotate on real hardware",
        "BLOCKED: no physical Android device provisioned; no signed-development "
        "release build installed; Activity recreation under rotation cannot be "
        "observed headless (config-destroy simulation passes in the harness).",
    ),
    (
        "keyboard-open on real hardware",
        "BLOCKED: no physical Android device; soft-keyboard resize/recreate "
        "behavior and composer clipping need on-device observation.",
    ),
    (
        "background / process kill on real hardware",
        "BLOCKED: no physical Android device; OS-initiated process death and "
        "activity-restore timing cannot be produced headlessly (simulated "
        "death + resume order pass in the harness).",
    ),
    (
        "force-stop on real hardware",
        "BLOCKED: no physical Android device; stopped-state FCM hold/drop and "
        "no-execution-until-relaunch are documented from the Android "
        "background-start reference, not observed. Nothing is claimed to work "
        "after force-stop until the user relaunches.",
    ),
    (
        "reboot on real hardware",
        "BLOCKED: no physical Android device; post-reboot provider "
        "re-registration and token validity need a real reboot cycle. No BOOT "
        "receiver is claimed.",
    ),
    (
        "Doze / battery restriction on real hardware",
        "BLOCKED: no physical Android device; Doze deferral and battery "
        "restriction effects on sync/push need on-device measurement. Push "
        "stays best-effort; the inbox stays the source of truth.",
    ),
    (
        "Wi-Fi/mobile switch on real hardware",
        "BLOCKED: no physical Android device and no dual-network radio "
        "environment; real radio handoff timing cannot be produced headless "
        "(scripted mid-stream switch + cursor dedupe pass in the harness).",
    ),
]


def check_lifecycle() -> None:
    check_pause_rule()
    check_resume_order()
    check_death_and_config()
    check_network_dedupe()
    check_storage_policy()
    check_home_independence()
    print("--- force-stop limits (documented, device proof blocked) ---")
    print(FORCE_STOP_LIMITS)
    print("--- real-device matrix ---")
    for name, reason in BLOCKED_MATRIX:
        print(f"[BLOCKED] {name}: {reason}")


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description="Headless device scenario checks.")
    parser.add_argument("--suite", required=True, help="lifecycle")
    parser.add_argument("--list", action="store_true", help="list suites")
    args = parser.parse_args(argv)
    if args.list or args.suite == "list":
        print("lifecycle")
        return 0
    if args.suite == "lifecycle":
        check_lifecycle()
    else:
        print(f"suite {args.suite!r} is not implemented for device checks")
        return 2
    print(f"{len(FAILURES)} failures")
    return 1 if FAILURES else 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
