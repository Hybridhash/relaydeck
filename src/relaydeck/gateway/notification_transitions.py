"""Status rules: baseline, changed blocked/done, three-second hold."""

from __future__ import annotations

import asyncio
import hashlib
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

MAX_TRACKED_AGENTS = 4096
HOLD_MS = 3000
MAX_ENQUEUE_ATTEMPTS = 3


@dataclass(frozen=True)
class Status:
    account: str
    host: str
    epoch: str
    session: str
    pane: str
    status: str
    event_id: str


class NotificationTransitions:
    def __init__(self) -> None:
        self.statuses: dict[tuple[str, str], Status] = {}
        self.pending: dict[tuple[str, str], tuple[int, Status]] = {}
        self.attempts: dict[tuple[str, str], int] = {}
        self.error = ""

    def drop_host(self, host: str) -> None:
        for key in list(self.statuses):
            if key[0] == host:
                self.statuses.pop(key, None)
                self.pending.pop(key, None)
                self.attempts.pop(key, None)

    def _observe(self, status: Status, now: int) -> None:
        if status.status not in {"idle", "working", "blocked", "done", "unknown"}:
            return
        key = (status.host, status.session)
        previous = self.statuses.get(key)
        if previous is None and len(self.statuses) >= MAX_TRACKED_AGENTS:
            return  # Push is best effort; memory admission is bounded.
        if (
            previous
            and previous.status == status.status
            and previous.epoch == status.epoch
            and previous.account == status.account
        ):
            # LiveSource publishes its refreshed snapshot before the event.
            # Confirm that held change with the stable source event identity.
            if key in self.pending and not previous.event_id and status.event_id:
                due, _ = self.pending[key]
                self.statuses[key] = status
                self.pending[key] = (due, status)
            return
        self.statuses[key] = status
        self.pending.pop(key, None)
        self.attempts.pop(key, None)
        if (
            previous
            and previous.account == status.account
            and previous.epoch == status.epoch
            and status.status in {"blocked", "done"}
        ):
            self.pending[key] = (now + HOLD_MS, status)

    def snapshot(self, account: str, host: str, snapshot: dict[str, Any], now: int) -> None:
        epoch = snapshot["host"]["epoch"]
        present: set[tuple[str, str]] = set()
        for agent in snapshot["agents"]:
            session = agent["terminal_id"]
            present.add((host, session))
            self._observe(
                Status(
                    account,
                    host,
                    epoch,
                    session,
                    agent.get("pane_id", session),
                    agent["status"],
                    "",  # Snapshot observations require source-event confirmation.
                ),
                now,
            )
        for key in list(self.statuses):
            if key[0] == host and key not in present:
                self.statuses.pop(key, None)
                self.pending.pop(key, None)
                self.attempts.pop(key, None)

    def event(self, account: str, host: str, event: dict[str, Any], now: int) -> None:
        kind, resource = event["type"], event.get("resource_id", "")
        if kind == "resource.removed":
            for key, previous in list(self.statuses.items()):
                if key[0] == host and resource in {previous.session, previous.pane}:
                    self.statuses.pop(key, None)
                    self.pending.pop(key, None)
                    self.attempts.pop(key, None)
            return
        if kind not in {"agent.upserted", "pane.upserted"}:
            return
        # The event schema makes payload optional; a valid event without one carries no status
        # and must not end the connector socket.
        payload = event.get("payload")
        if not isinstance(payload, dict):
            return
        value = payload.get("status" if kind == "agent.upserted" else "agent_status")
        if not isinstance(value, str):
            return
        for previous in list(self.statuses.values()):
            if previous.host == host and resource in {previous.session, previous.pane}:
                self._observe(
                    Status(
                        account,
                        host,
                        event["host_epoch"],
                        previous.session,
                        previous.pane,
                        value,
                        "transition_"
                        + hashlib.sha256(
                            repr(
                                (host, event["host_epoch"], previous.session, event["event_id"])
                            ).encode()
                        ).hexdigest(),
                    ),
                    now,
                )

    def flush(self, now: int, emit: Callable[[Status, str], Any]) -> None:
        for key, (due, status) in list(self.pending.items()):
            if due > now:
                continue
            if self.statuses.get(key) != status or not status.event_id:
                self.pending.pop(key, None)
                self.attempts.pop(key, None)
                continue
            try:
                emit(status, "attention" if status.status == "blocked" else "completion")
            except Exception:  # noqa: BLE001 - bounded enqueue retry, redacted diagnostics
                count = self.attempts.get(key, 0) + 1
                self.error = "notification_outbox_unavailable"
                self.attempts[key] = count
                if count < MAX_ENQUEUE_ATTEMPTS:
                    self.pending[key] = (now + 250 * count, status)
                    continue
                self.error = "notification_enqueue_exhausted"
            self.pending.pop(key, None)
            self.attempts.pop(key, None)

    async def flush_async(self, now: int, emit: Callable[[Status, str], Awaitable[None]]) -> None:
        """Keep transition state on the loop; an older completion cannot settle a newer event."""
        for key, entry in list(self.pending.items()):
            due, status = entry
            if due > now:
                continue
            if self.statuses.get(key) != status or not status.event_id:
                if self.pending.get(key) == entry:
                    self.pending.pop(key, None)
                    self.attempts.pop(key, None)
                continue
            failed = False
            try:
                await emit(status, "attention" if status.status == "blocked" else "completion")
            except Exception:  # noqa: BLE001 - fixed bounded retry, no payload diagnostics
                failed = True
            if self.pending.get(key) != entry or self.statuses.get(key) != status:
                continue
            if failed:
                count = self.attempts.get(key, 0) + 1
                self.error = "notification_outbox_unavailable"
                self.attempts[key] = count
                if count < MAX_ENQUEUE_ATTEMPTS:
                    self.pending[key] = (now + 250 * count, status)
                    continue
                self.error = "notification_enqueue_exhausted"
            self.pending.pop(key, None)
            self.attempts.pop(key, None)

    async def run(
        self,
        stop: asyncio.Event,
        clock: Callable[[], int],
        emit: Callable[[Status, str], Awaitable[None]],
    ) -> None:
        try:
            while not stop.is_set():
                try:
                    await self.flush_async(clock(), emit)
                except Exception:  # noqa: BLE001 - isolate push storage from source synchronization
                    self.error = "notification_outbox_unavailable"
                try:
                    async with asyncio.timeout(0.25):
                        await stop.wait()
                except TimeoutError:
                    pass
        finally:
            self.pending.clear()
            self.statuses.clear()
            self.attempts.clear()
