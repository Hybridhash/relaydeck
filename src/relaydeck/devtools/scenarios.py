"""Named fixture scenarios + dev-only selector gate.

Scenarios compose the fake adapters into the situations the UI must
handle. The fixture selector is available only when explicitly enabled
for development; production builds cannot enable it.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field

from relaydeck.client.adapters.ports import FakeClock
from relaydeck.devtools.fake_gateway import FakeGatewayAdapter
from relaydeck.devtools.fake_host import FakeHostAdapter
from relaydeck.devtools.fake_terminal import FakeTerminalStream
from relaydeck.domain.models import CommandStatus, EventType


def fixture_selector_enabled() -> bool:
    """Dev-only gate. Requires RELAYDECK_FIXTURES=1 and forbids production."""
    if os.environ.get("RELAYDECK_PRODUCTION") == "1":
        return False
    return os.environ.get("RELAYDECK_FIXTURES") == "1"


@dataclass(frozen=True)
class Scenario:
    name: str
    description: str


SCENARIOS: tuple[Scenario, ...] = (
    Scenario("steady", "Live host: working/blocked/done agents, two shells."),
    Scenario("takeover", "Terminal owned by another viewer; takeover offered."),
    Scenario("slow-output", "Output drips in small chunks with stalls."),
    Scenario("duplicates", "Duplicate metadata deliveries (idempotent)."),
    Scenario("host-offline", "Upstream disconnect then resync on return."),
    Scenario("unknown-command", "Command outcome unknown; explicit resolve."),
)


@dataclass
class ScenarioWorld:
    clock: FakeClock = field(default_factory=FakeClock)
    host: FakeHostAdapter = field(default_factory=FakeHostAdapter)
    gateway: FakeGatewayAdapter = field(default_factory=FakeGatewayAdapter)
    terminals: dict[str, FakeTerminalStream] = field(default_factory=dict)

    def terminal(self, terminal_id: str) -> FakeTerminalStream:
        stream = self.terminals.get(terminal_id)
        if stream is None:
            stream = FakeTerminalStream(terminal_id=terminal_id, clock=self.clock)
            self.terminals[terminal_id] = stream
        return stream


def build_scenario(name: str) -> ScenarioWorld:
    world = ScenarioWorld()
    world.host.clock = world.clock
    world.gateway.clock = world.clock
    if name == "steady":
        pass
    elif name == "takeover":
        stream = world.terminal("term-working")
        stream.attach("other-viewer", 1)
    elif name == "slow-output":
        stream = world.terminal("term-working")
        stream.load(b"x" * 100_000)
        stream.chunk_size = 64
    elif name == "duplicates":
        event = world.host.emit(
            EventType.AGENT_UPSERTED,
            resource_id="term-working",
            revision=11,
            payload={"status": "working"},
        )
        world.host.queue(event)
    elif name == "host-offline":
        world.host.offline = True
    elif name == "unknown-command":
        world.gateway.script("cmd-unknown", CommandStatus.UNKNOWN)
    else:
        raise ValueError(f"unknown scenario {name}")
    return world


def describe_scenarios() -> tuple[Scenario, ...]:
    if not fixture_selector_enabled():
        return ()
    return SCENARIOS
