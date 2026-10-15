"""Bounded-output, navigation, and reconnect benchmarks.

Headless measurements over the REAL paths (no Flutter, no browser):

- vt_burst: a synthetic ~6 MiB VT burst (100 KiB/s x 60 s
  worth of bytes) through producer -> ring -> wire framing -> client
  intake -> projection, with a drained viewer and a stalled viewer.
  Asserts ring/viewer/scrollback bounds and records memory + timing.
- navigation: inventory render + select + filter through the host
  harness (headless proxy for the 3 s initial-view budget,
  which needs a real browser at the release gate).
- reconnect: SyncController drop -> LIVE recovery with a scripted
  transport (headless proxy for the 5 s foreground-reconnect
  budget, which needs network/device proof at the release gate).

Prints a measurements table; exits nonzero when a budget is breached.
Bounds from the ring design (2 MiB ring, 1 MiB viewer, 5000-line
scrollback) are hard requirements; timing/memory tripwires are newly
established (justified below) since no device measurements exist yet.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import resource
import sys
import time
import tracemalloc
from dataclasses import dataclass
from typing import Any

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from relaydeck.client.app import Root  # noqa: E402
from relaydeck.client.features.inventory.components import AppShell  # noqa: E402
from relaydeck.client.features.inventory.routing import RouteBridge  # noqa: E402
from relaydeck.client.features.sync.backoff import Backoff  # noqa: E402
from relaydeck.client.features.sync.controller import SyncController  # noqa: E402
from relaydeck.client.features.sync.transport import (  # noqa: E402
    GatewayTransport,
    TransportError,
)
from relaydeck.client.features.terminal.projection import TerminalProjection  # noqa: E402
from relaydeck.client.features.terminal.stream import TerminalByteStream  # noqa: E402
from relaydeck.domain import reducers  # noqa: E402
from relaydeck.domain.models import (  # noqa: E402
    Agent,
    AgentStatus,
    AppState,
    Connection,
    ConnectionState,
    EventType,
    Host,
    Inventory,
    MetadataEvent,
    Selection,
    Tab,
    Workspace,
)
from relaydeck.gateway import terminal_wire  # noqa: E402
from relaydeck.gateway.ring import (  # noqa: E402
    RING_MAX_BYTES,
    VIEWER_QUEUE_MAX_BYTES,
    StreamFanout,
)
from tests.support.host_harness import render_component  # noqa: E402
from tests.support.scripted_sessions import make_scripted_sessions  # noqa: E402

FIXTURES = os.path.join(os.path.dirname(__file__), "..", "tests", "fixtures")

# Newly-established tripwires (no device data yet): generous ceilings that
# catch unbounded growth / pathological slowness, not tuned SLOs.
BUDGET_TRACEMALLOC_PEAK_MIB = 64.0
BUDGET_RSS_DELTA_MIB = 96.0
BUDGET_BURST_WALL_S = 30.0
BUDGET_NAV_RENDER_S = 1.0
BUDGET_RECONNECT_COMPUTE_S = 1.0
BUDGET_METADATA_BURST_S = 1.0

BURST_MIB = 6
WIRE_CHUNK = 64 * 1024


@dataclass
class Measurement:
    name: str
    value: str
    budget: str
    ok: bool


RESULTS: list[Measurement] = []


def record(name: str, value: str, budget: str, ok: bool) -> None:
    RESULTS.append(Measurement(name, value, budget, ok))


def _rss_mib() -> float:
    peak = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    scale = 1 if sys.platform == "darwin" else 1024
    return peak * scale / (1024 * 1024)


def _burst_block(index: int) -> bytes:
    """One deterministic 64 KiB VT block: SGR lines plus periodic cursor
    addressing and erase, newline-terminated like real tool output."""
    lines: list[bytes] = []
    size = 0
    line_no = index * 1000
    while size < WIRE_CHUNK - 128:
        line_no += 1
        payload = f"[{line_no:08d}] synthetic burst output; all bytes preserved. ".encode()
        payload += b"0123456789abcdef" * 4
        if line_no % 50 == 0:
            lines.append(b"\x1b[10;20H\x1b[K")
        lines.append(b"\x1b[32m" + payload + b"\x1b[0m\r\n")
        size += len(lines[-1])
    block = b"".join(lines)
    return block[:WIRE_CHUNK]


def bench_vt_burst() -> None:
    total = BURST_MIB * 1024 * 1024
    rss_before = _rss_mib()
    tracemalloc.start()
    blocks = [_burst_block(i) for i in range(total // WIRE_CHUNK)]
    assert sum(len(b) for b in blocks) == total

    fanout = StreamFanout("bench-epoch", 0)
    fast = fanout.subscribe("fast")
    slow = fanout.subscribe("slow")
    stream = TerminalByteStream()
    projection = TerminalProjection()

    start = time.perf_counter()
    offset = 0
    for tick, raw in enumerate(blocks):
        fanout.publish(raw, tick)
        fast.pop_all()
        frame = terminal_wire.encode_output("bench-epoch", offset, raw)
        decoded = terminal_wire.decode_output(frame)
        event = stream.push(decoded)
        assert event.kind == "data", event
        projection.feed(event.data)
        offset += len(raw)
    projection.flush()
    wall = time.perf_counter() - start
    _, traced_peak = tracemalloc.get_traced_memory()
    tracemalloc.stop()
    rss_after = _rss_mib()

    mib = total / (1024 * 1024)
    throughput = mib / wall if wall > 0 else 0.0
    traced_peak_mib = traced_peak / (1024 * 1024)
    rss_delta = max(0.0, rss_after - rss_before)

    record("vt_burst.bytes_total", f"{mib:.1f} MiB", f"{BURST_MIB} MiB in", True)
    record(
        "vt_burst.stream_offset",
        str(stream.next_offset),
        f"== {total} (no loss)",
        stream.next_offset == total,
    )
    record(
        "vt_burst.ring_retained",
        f"{fanout.ring.retained_bytes / 1024 / 1024:.2f} MiB",
        f"<= {RING_MAX_BYTES / 1024 / 1024:.0f} MiB (design)",
        fanout.ring.retained_bytes <= RING_MAX_BYTES,
    )
    record(
        "vt_burst.slow_viewer_overflow",
        f"overflowed={slow.overflowed} pending={slow.pending_bytes // 1024} KiB",
        f"explicit, <= {VIEWER_QUEUE_MAX_BYTES // 1024} KiB (design)",
        slow.overflowed and slow.pending_bytes <= VIEWER_QUEUE_MAX_BYTES,
    )
    record(
        "vt_burst.fast_viewer_pending", f"{fast.pending_bytes} B", "== 0", fast.pending_bytes == 0
    )
    record(
        "vt_burst.scrollback_lines",
        str(len(projection.scrollback)),
        "<= 5000 (design)",
        len(projection.scrollback) <= 5000,
    )
    record(
        "vt_burst.screen_rows",
        str(len(projection.lines)),
        "<= 24",
        len(projection.lines) <= 24,
    )
    record(
        "vt_burst.tracemalloc_peak",
        f"{traced_peak_mib:.1f} MiB",
        f"< {BUDGET_TRACEMALLOC_PEAK_MIB:.0f} MiB (tripwire)",
        traced_peak_mib < BUDGET_TRACEMALLOC_PEAK_MIB,
    )
    record(
        "vt_burst.rss_delta",
        f"{rss_delta:.1f} MiB",
        f"< {BUDGET_RSS_DELTA_MIB:.0f} MiB (tripwire)",
        rss_delta < BUDGET_RSS_DELTA_MIB,
    )
    record(
        "vt_burst.wall_s",
        f"{wall:.3f}",
        f"< {BUDGET_BURST_WALL_S:.0f} (tripwire)",
        wall < BUDGET_BURST_WALL_S,
    )
    # Throughput is informational: the projection oracle is pure-Python
    # test tooling, while production renders natively (xterm2). The
    # release-relevant budgets are the bounds above plus the hang guard.
    record("vt_burst.throughput", f"{throughput:.2f} MiB/s", "info (oracle speed)", True)


def _bench_state() -> AppState:
    hosts = {"home-mac": Host(id="home-mac", label="Home Mac", epoch="epoch-7", reachable=True)}
    workspaces = {"w1": Workspace(id="w1", host_id="home-mac", label="web")}
    tabs = {"tab-1": Tab(id="tab-1", host_id="home-mac", workspace_id="w1", label="shell-a")}
    agents = {
        f"term-{i}": Agent(
            terminal_id=f"term-{i}",
            host_id="home-mac",
            workspace_id="w1",
            tab_id="tab-1",
            pane_id="p1",
            name=f"agent-{i}",
            agent="tool-a",
            status=AgentStatus.WORKING if i % 2 else AgentStatus.BLOCKED,
            focused=(i == 0),
        )
        for i in range(20)
    }
    return AppState(
        connection=Connection(state=ConnectionState.LIVE, host_id="home-mac", cursor="c" * 32),
        inventory=Inventory(hosts=hosts, workspaces=workspaces, tabs=tabs, panes={}, agents=agents),
        selection=Selection(host_id="home-mac", session_key="term-0"),
    )


class _StubController:
    def __init__(self) -> None:
        self.listeners: list[Any] = []
        self.backoff = Backoff.seeded(1)
        self.state = AppState()

    def subscribe(self, listener: Any) -> None:
        self.listeners.append(listener)

    def unsubscribe(self, listener: Any) -> None:
        if listener in self.listeners:
            self.listeners.remove(listener)

    async def run(self, host_id: str, stop: Any = lambda: False) -> None:
        while not stop():
            await asyncio.sleep(3600)

    def push(self, state: AppState) -> None:
        self.state = state
        for listener in self.listeners:
            listener(state)

    def apply_grants(self, entries: list[dict[str, Any]]) -> AppState:
        """Mirror `SyncController.apply_grants` (grant ownership)."""
        self.push(reducers.merge_hosts(self.state, entries))
        return self.state

    def select(self, host_id: str, session_key: str, terminal_kind: str) -> AppState:
        """Mirror `SyncController.select` (selection ownership)."""
        self.push(reducers.select_session(self.state, host_id, session_key, terminal_kind))
        return self.state


class _StubTransport:
    async def submit_command(self, command: dict[str, Any]) -> dict[str, Any]:
        return {"command_id": command["command_id"], "status": "accepted"}


def _noop(*args: Any) -> None:
    pass


def bench_navigation() -> None:
    async def scenario() -> None:
        state = _bench_state()
        worst = 0.0
        for destination in ("agents", "terminals", "hosts", "settings"):
            for wide in (True, False):
                start = time.perf_counter()
                app = await render_component(
                    AppShell,
                    state=state,
                    is_wide=wide,
                    destination=destination,
                    on_destination=_noop,
                    on_select_agent=_noop,
                    on_select_shell=_noop,
                    on_select_host=_noop,
                    on_banner_action=_noop,
                    on_open_terminal=_noop,
                    on_start_agent=_noop,
                    on_new_terminal=_noop,
                )
                assert app.find("destination-title").value
                worst = max(worst, time.perf_counter() - start)
        record(
            "navigation.render_worst_s",
            f"{worst:.3f}",
            f"< {BUDGET_NAV_RENDER_S:.1f} (headless tripwire; the 3 s budget needs a browser)",
            worst < BUDGET_NAV_RENDER_S,
        )

        controller = _StubController()
        bridge = RouteBridge(route="/", width=1280)
        pushed: list[str] = []
        app = await render_component(
            Root,
            controller=controller,
            transport=_StubTransport(),
            host_id="home-mac",
            bridge=bridge,
            go=pushed.append,
            session_factory=make_scripted_sessions().factory,
            account="bench",
        )
        controller.push(state)
        await app.settle()
        start = time.perf_counter()
        await app.set_props_and_fire("agent-term-1", {}, "click")
        select_wall = time.perf_counter() - start
        assert pushed == ["/agents/term-1"]
        record(
            "navigation.select_route_s",
            f"{select_wall:.3f}",
            f"< {BUDGET_NAV_RENDER_S:.1f} (tripwire)",
            select_wall < BUDGET_NAV_RENDER_S,
        )
        start = time.perf_counter()
        await app.set_props_and_fire("inventory-query", {"value": "agent-1"}, "change")
        filter_wall = time.perf_counter() - start
        assert app.find("inventory-query").value == "agent-1"
        record(
            "navigation.filter_s",
            f"{filter_wall:.3f}",
            f"< {BUDGET_NAV_RENDER_S:.1f} (tripwire)",
            filter_wall < BUDGET_NAV_RENDER_S,
        )

    asyncio.run(scenario())


class _DropOnceTransport(GatewayTransport):
    """Real snapshot fixture; the first stream drops, later streams end."""

    def __init__(self, snapshot: dict[str, Any]) -> None:
        self._snapshot = snapshot
        self.snapshots = 0
        self.streams = 0
        self._fail_next = True

    async def me(self) -> dict[str, Any]:
        raise NotImplementedError

    async def hosts(self) -> dict[str, Any]:
        raise NotImplementedError

    async def snapshot(self, host_id: str) -> dict[str, Any]:
        self.snapshots += 1
        return json.loads(json.dumps(self._snapshot))

    async def agents(self, host_id: str, limit: int = 50, page_token: str = "") -> dict[str, Any]:
        raise NotImplementedError

    async def stream(self, host_id: str, cursor: str):  # type: ignore[override]
        self.streams += 1
        if self._fail_next:
            self._fail_next = False
            raise TransportError("link_down", "link dropped", True)
            yield  # pragma: no cover - keep this an async generator
        return

    async def submit_command(self, command: dict[str, Any]) -> dict[str, Any]:
        raise NotImplementedError

    async def command_status(self, host_id: str, command_id: str) -> dict[str, Any] | None:
        raise NotImplementedError

    async def terminal_ticket(self, lease_id: str) -> dict[str, Any]:
        raise NotImplementedError


def bench_reconnect() -> None:
    with open(os.path.join(FIXTURES, "gateway", "snapshot.json"), encoding="utf-8") as fh:
        snapshot = json.load(fh)

    async def scenario() -> None:
        transport = _DropOnceTransport(snapshot)
        slept: list[float] = []

        async def sleep(delay: float) -> None:
            slept.append(delay)

        controller = SyncController(transport=transport, backoff=Backoff.seeded(7), sleep=sleep)
        start = time.perf_counter()
        final = await controller.run("home-mac")
        wall = time.perf_counter() - start
        assert final.connection.state == ConnectionState.LIVE, final.connection.state
        assert (transport.snapshots, transport.streams) == (2, 2)
        record(
            "reconnect.drop_to_live_s",
            f"{wall:.3f}",
            f"< {BUDGET_RECONNECT_COMPUTE_S:.1f} compute "
            "(tripwire; the 5 s budget needs a network)",
            wall < BUDGET_RECONNECT_COMPUTE_S,
        )
        record(
            "reconnect.backoff_first_ms",
            f"{slept[0] * 1000:.0f}" if slept else "n/a",
            "scheduled, stubbed sleep",
            True,
        )

        # Metadata-apply burst: 500 chained events through the real reducers.
        epoch = snapshot["host"]["epoch"]
        base_seq = 1000
        start = time.perf_counter()
        state = final
        for i in range(500):
            event = MetadataEvent(
                event_id=f"ev-bench-{i}",
                host_id="home-mac",
                host_epoch=epoch,
                sequence=base_seq + i + 1,
                cursor=f"cursor-{i}",
                type=EventType("agent.upserted"),
                resource_id=f"term-bench-{i % 25}",
                resource_revision=i + 1,
                observed_ms=0,
                payload={"status": "working", "name": f"bench-{i % 25}", "agent": "tool-a"},
            )
            if i == 0:
                # Chain onto the snapshot cursor: first event continues it.
                state = reducers.set_connection(state, ConnectionState.LIVE)
            state, _ = reducers.apply_event(state, event)
        burst_wall = time.perf_counter() - start
        record(
            "reconnect.metadata_500_s",
            f"{burst_wall:.3f}",
            f"< {BUDGET_METADATA_BURST_S:.1f} (tripwire)",
            burst_wall < BUDGET_METADATA_BURST_S,
        )

    asyncio.run(scenario())


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description="RelayDeck terminal benchmarks.")
    parser.add_argument(
        "--fixture",
        default="all",
        choices=("vt_burst", "navigation", "reconnect", "all"),
        help="benchmark to run",
    )
    args = parser.parse_args(argv)
    if args.fixture in ("vt_burst", "all"):
        bench_vt_burst()
    if args.fixture in ("navigation", "all"):
        bench_navigation()
    if args.fixture in ("reconnect", "all"):
        bench_reconnect()

    width = max(len(m.name) for m in RESULTS)
    print(f"{'measurement':<{width}}  {'value':<24}  {'budget':<52}  status")
    print(f"{'-' * width}  {'-' * 24}  {'-' * 52}  ------")
    failed = 0
    for m in RESULTS:
        print(f"{m.name:<{width}}  {m.value:<24}  {m.budget:<52}  {'PASS' if m.ok else 'FAIL'}")
        failed += not m.ok
    print(f"{failed} budget breaches")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
