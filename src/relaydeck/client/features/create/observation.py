"""Observe one creation identity; resource reads cannot cause another launch."""

from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from relaydeck.client.features.composer.observation import CommandObserver, CommandOutcomeUnknown
from relaydeck.contracts.schemas import validate
from relaydeck.domain.models import scope_key


@dataclass(frozen=True)
class CreatedResource:
    host_id: str
    kind: str
    resource_id: str
    snapshot: dict[str, Any] | None = None

    @property
    def key(self) -> str:
        return scope_key(self.host_id, self.resource_id)


class CreationObserver:
    def __init__(
        self,
        status: Callable[[str, str], Awaitable[dict[str, Any] | None]],
        snapshot: Callable[[str], Awaitable[dict[str, Any]]],
        *,
        visibility_s: float = 5,
        interval_s: float = 0.2,
        command_budget_s: float = 30,
    ) -> None:
        self._commands = CommandObserver(status, budget_s=command_budget_s, interval_s=interval_s)
        self._snapshot = snapshot
        self._visibility = visibility_s
        self._interval = interval_s

    async def observe(self, command: dict[str, Any]) -> CreatedResource:
        record = await self._commands.observe(command["host_id"], command["command_id"])
        result = record.get("result", {})
        agent = command["kind"] == "agent.start"
        expected = "agent_started" if agent else "tab_created"
        identity = result.get("agent_id" if agent else "tab_id")
        if (
            record["kind"] != command["kind"]
            or record["target"] != command["target"]
            or result.get("type") != expected
            or not identity
        ):
            raise CommandOutcomeUnknown(
                "Creation outcome has no verified resource identity; do not retry."
            )
        resource = CreatedResource(command["host_id"], "agent" if agent else "shell", identity)
        deadline = time.monotonic() + self._visibility
        while time.monotonic() < deadline:
            try:
                async with asyncio.timeout(max(0.001, deadline - time.monotonic())):
                    doc = await self._snapshot(resource.host_id)
                validate("relaydeck/gateway/v1/snapshot", doc)
            except Exception:
                # A known successful creation remains known if its inventory is late.
                return resource
            if (
                doc["host"]["id"] != resource.host_id
                or doc["host"]["epoch"] != command["host_epoch"]
            ):
                raise CommandOutcomeUnknown(
                    "Host changed after creation; refresh before opening it."
                )
            group, key = ("agents", "terminal_id") if agent else ("tabs", "id")
            if any(
                row[key] == identity and row["host_id"] == resource.host_id for row in doc[group]
            ):
                return CreatedResource(resource.host_id, resource.kind, resource.resource_id, doc)
            await asyncio.sleep(min(self._interval, max(0, deadline - time.monotonic())))
        return resource
