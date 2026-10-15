"""Fake host adapter: scripted snapshots and metadata events.

Deterministic inventory shapes for development and tests: running,
blocked, and completed agents plus two shell tabs. Scripts advance on a
manual clock so tests never sleep.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from relaydeck.client.adapters.ports import FakeClock
from relaydeck.domain.models import (
    Agent,
    AgentStatus,
    EventType,
    MetadataEvent,
    Pane,
    Snapshot,
    Tab,
    Workspace,
)

HOST_ID = "fake-host-1"
HOST_EPOCH = "fake-epoch-1"


class HostOfflineError(RuntimeError):
    pass


def demo_snapshot(now_ms: int = 1_000_000) -> Snapshot:
    return Snapshot(
        host_id=HOST_ID,
        host_epoch=HOST_EPOCH,
        cursor="snap-1",
        workspaces=(
            Workspace(
                id="w1",
                host_id=HOST_ID,
                number=1,
                label="demo",
                focused=True,
                pane_count=3,
                tab_count=3,
                active_tab_id="w1:t1",
                agent_status=AgentStatus.WORKING,
            ),
        ),
        tabs=(
            Tab(
                id="w1:t1",
                host_id=HOST_ID,
                workspace_id="w1",
                number=1,
                label="Agent · feature",
                focused=True,
                pane_count=1,
                agent_status=AgentStatus.WORKING,
            ),
            Tab(
                id="w1:t2",
                host_id=HOST_ID,
                workspace_id="w1",
                number=2,
                label="shell",
                pane_count=1,
                agent_status=AgentStatus.UNKNOWN,
            ),
            Tab(
                id="w1:t3",
                host_id=HOST_ID,
                workspace_id="w1",
                number=3,
                label="shell",
                pane_count=1,
                agent_status=AgentStatus.UNKNOWN,
            ),
        ),
        panes=(
            Pane(
                id="w1:p1",
                host_id=HOST_ID,
                workspace_id="w1",
                tab_id="w1:t1",
                focused=True,
                agent_status=AgentStatus.WORKING,
            ),
            Pane(id="w1:p2", host_id=HOST_ID, workspace_id="w1", tab_id="w1:t2"),
            Pane(id="w1:p3", host_id=HOST_ID, workspace_id="w1", tab_id="w1:t3"),
        ),
        agents=(
            Agent(
                terminal_id="term-working",
                host_id=HOST_ID,
                workspace_id="w1",
                tab_id="w1:t1",
                pane_id="w1:p1",
                name="feature-agent",
                agent="agent-a",
                title="implement feature | demo",
                status=AgentStatus.WORKING,
                focused=True,
                interactive_ready=True,
                state_change_seq=4,
                revision=10,
            ),
            Agent(
                terminal_id="term-blocked",
                host_id=HOST_ID,
                workspace_id="w1",
                tab_id="w1:t1",
                pane_id="w1:p1",
                name="review-agent",
                agent="agent-a",
                title="awaiting approval | demo",
                status=AgentStatus.BLOCKED,
                interactive_ready=True,
                state_change_seq=9,
                revision=12,
            ),
            Agent(
                terminal_id="term-done",
                host_id=HOST_ID,
                workspace_id="w1",
                tab_id="w1:t1",
                pane_id="w1:p1",
                name="done-agent",
                agent="agent-a",
                title="finished task | demo",
                status=AgentStatus.DONE,
                interactive_ready=True,
                state_change_seq=15,
                revision=20,
            ),
        ),
        taken_ms=now_ms,
    )


@dataclass
class FakeHostAdapter:
    clock: FakeClock = field(default_factory=FakeClock)
    offline: bool = False
    epoch: str = HOST_EPOCH
    _sequence: int = 0
    _scripts: list[MetadataEvent] = field(default_factory=list)

    def snapshot(self) -> Snapshot:
        if self.offline:
            raise HostOfflineError("fake host offline")
        return demo_snapshot(now_ms=self.clock.now_ms)

    def queue(self, event: MetadataEvent) -> None:
        self._scripts.append(event)

    def poll(self, cursor: str) -> tuple[str, tuple[MetadataEvent, ...]]:
        """Return (new_cursor, events after cursor). Cursor format: seq-N."""
        if self.offline:
            raise HostOfflineError("fake host offline")
        try:
            after = int(cursor.split("-")[1]) if cursor else 0
        except (IndexError, ValueError):
            after = 0
        due = tuple(e for e in self._scripts if e.sequence > after)
        if not due:
            return cursor, ()
        return f"seq-{due[-1].sequence}", due

    def emit(
        self,
        type: EventType,
        resource_id: str = "",
        revision: int = 0,
        payload: dict[str, Any] | None = None,
    ) -> MetadataEvent:
        self._sequence += 1
        event = MetadataEvent(
            event_id=f"fake-{self._sequence}",
            host_id=HOST_ID,
            host_epoch=self.epoch,
            sequence=self._sequence,
            cursor=f"seq-{self._sequence}",
            type=type,
            resource_id=resource_id,
            resource_revision=revision,
            observed_ms=self.clock.now_ms,
            payload=payload or {},
        )
        self._scripts.append(event)
        return event

    def rotate_epoch(self) -> None:
        self.epoch = f"{HOST_EPOCH}-rotated"
        self._sequence = 0
        self._scripts.clear()
