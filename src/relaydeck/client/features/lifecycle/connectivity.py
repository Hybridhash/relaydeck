"""Network-switch resynchronization. Flet-free.

A connectivity change (Wi-Fi/mobile/offline) is a signal to resync, not
a state to display: `LinkResync` subscribes to the `ConnectivityPort`
and invokes the registered resync callback (production wires
`SyncController.request_resync`, whose retryable `link_change` error
drives the existing snapshot + cursor-resume path). The switch itself
duplicates nothing: resume is by cursor and the reducers treat
replayed sequences as idempotent duplicates (`CursorDeduper` models the
same rule for tests that bypass the reducers).
"""

from __future__ import annotations

from collections import deque
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from relaydeck.client.adapters.ports import ConnectivityPort, ConnectivityState


@dataclass(frozen=True)
class LinkEvent:
    """One observed connectivity change, in arrival order."""

    seq: int
    state: ConnectivityState


class CursorDeduper:
    """Cursor/sequence dedupe for reconnect replays.

    The sync reducers already treat `sequence <= last_sequence` as an
    idempotent duplicate; this helper applies the same rule to raw event
    dicts so transport-level tests can assert a network switch duplicates
    nothing without driving the full controller.
    """

    def __init__(self) -> None:
        self._seen_cursors: set[str] = set()
        self._last_sequence = 0
        self.duplicates = 0

    @property
    def last_sequence(self) -> int:
        return self._last_sequence

    def filter(self, events: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Return the unseen events in order; count the duplicates."""
        fresh: list[dict[str, Any]] = []
        for event in events:
            cursor = str(event.get("cursor", ""))
            sequence = int(event.get("sequence", 0))
            if (cursor and cursor in self._seen_cursors) or (
                sequence and sequence <= self._last_sequence
            ):
                self.duplicates += 1
                continue
            if cursor:
                self._seen_cursors.add(cursor)
            if sequence > self._last_sequence:
                self._last_sequence = sequence
            fresh.append(event)
        return fresh


#: Diagnostic history bound: LinkResync retains only the latest N link
#: events. The resync callback still fires on EVERY change; only the
#: retained history is bounded, so a flapping radio cannot grow memory
#: without limit. Sequence numbers stay monotonic across eviction.
LINK_EVENT_HISTORY = 32


class LinkResync:
    """Connectivity-change signal -> explicit transport resync.

    Records the latest link events (bounded history) and invokes
    `on_link_change` (with the new state) on EVERY change, so the sync
    supervisor requests a resync even when the old sockets are half-open
    and would not error promptly on their own. Rapid signals are all
    delivered; the controller coalesces them into a single recovery.
    """

    def __init__(
        self,
        port: ConnectivityPort,
        on_link_change: Callable[[ConnectivityState], None],
    ) -> None:
        self._port = port
        self._callback = on_link_change
        self.events: deque[LinkEvent] = deque(maxlen=LINK_EVENT_HISTORY)
        self._seq = 0
        port.on_change(self._handle)

    @property
    def current(self) -> ConnectivityState:
        return self._port.current()

    def _handle(self, state: ConnectivityState) -> None:
        self._seq += 1
        self.events.append(LinkEvent(seq=self._seq, state=state))
        self._callback(state)
