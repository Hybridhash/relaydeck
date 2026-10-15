"""Fake gateway adapter: scripted command outcomes.

Commands move through accepted -> dispatched -> terminal state on the
manual clock. Scripted outcomes include success, failure, rejection
(capability/validation), and unknown (stays unknown; never auto-retried).
"""

from __future__ import annotations

from dataclasses import dataclass, field

from relaydeck.client.adapters.ports import FakeClock
from relaydeck.domain.models import CommandRecord, CommandStatus


@dataclass(frozen=True)
class CommandIntent:
    command_id: str
    host_id: str
    kind: str
    target: dict[str, str]
    payload_digest: str = ""


@dataclass
class _Script:
    states: list[CommandStatus]
    error_code: str = ""


@dataclass
class FakeGatewayAdapter:
    clock: FakeClock = field(default_factory=FakeClock)
    capabilities: frozenset[str] = frozenset(
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
    _commands: dict[str, CommandRecord] = field(default_factory=dict)
    _scripts: dict[str, _Script] = field(default_factory=dict)
    _ticks: dict[str, int] = field(default_factory=dict)

    def script(self, command_id: str, *states: CommandStatus, error_code: str = "") -> None:
        self._scripts[command_id] = _Script(list(states), error_code)
        self._ticks[command_id] = 0

    def submit(self, intent: CommandIntent) -> CommandRecord:
        current = self._commands.get(intent.command_id)
        if current is not None:
            if current.payload_digest == intent.payload_digest:
                return current
            raise ValueError(f"command id conflict: {intent.command_id}")
        record = CommandRecord(
            command_id=intent.command_id,
            host_id=intent.host_id,
            kind=intent.kind,
            target=intent.target,
            status=CommandStatus.ACCEPTED,
            payload_digest=intent.payload_digest,
            created_ms=self.clock.now_ms,
            updated_ms=self.clock.now_ms,
        )
        self._commands[intent.command_id] = record
        self._ticks.setdefault(intent.command_id, 0)
        return record

    def advance(self) -> tuple[CommandRecord, ...]:
        """Move each command one scripted step; returns changed records."""
        changed: list[CommandRecord] = []
        for cid, record in list(self._commands.items()):
            script = self._scripts.get(cid)
            tick = self._ticks.get(cid, 0)
            if script is None:
                if record.status == CommandStatus.ACCEPTED:
                    updated = CommandRecord(
                        command_id=record.command_id,
                        host_id=record.host_id,
                        kind=record.kind,
                        target=record.target,
                        status=CommandStatus.DISPATCHED,
                        payload_digest=record.payload_digest,
                        created_ms=record.created_ms,
                        updated_ms=self.clock.now_ms,
                    )
                    self._commands[cid] = updated
                    changed.append(updated)
                continue
            if tick >= len(script.states):
                continue
            status = script.states[tick]
            self._ticks[cid] = tick + 1
            updated = CommandRecord(
                command_id=record.command_id,
                host_id=record.host_id,
                kind=record.kind,
                target=record.target,
                status=status,
                payload_digest=record.payload_digest,
                error_code=script.error_code if status == CommandStatus.FAILED else "",
                created_ms=record.created_ms,
                updated_ms=self.clock.now_ms,
            )
            self._commands[cid] = updated
            changed.append(updated)
        return tuple(changed)

    def get(self, command_id: str) -> CommandRecord | None:
        return self._commands.get(command_id)

    def revoke_capability(self, capability: str) -> None:
        self.capabilities = self.capabilities - {capability}

    def check(self, kind: str, capability: str) -> str | None:
        """Return an error code when the capability is missing."""
        if capability not in self.capabilities:
            return f"capability_required:{capability}"
        return None
