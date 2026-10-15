"""Ordered resume flow (Flet-free): identity, route, pending commands, snapshot, cursor, control.

Each step awaits the previous one and control is granted only by the last. Any
step failure aborts with `ResumeError`: later steps never run and control is
never granted. Pending commands are only re-queried, never resent; an unknown
status stays unknown. A successful report may return to a read-only foreground
when explicit takeover confirmation is still required.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

from relaydeck.client.features.inventory.routing import RouteState, parse_route


class ResumeStep(Enum):
    REFRESH_IDENTITY = "refresh_identity"
    RESTORE_ROUTE = "restore_route"
    QUERY_PENDING = "query_pending"
    FETCH_SNAPSHOT = "fetch_snapshot"
    RECONNECT_CURSOR = "reconnect_cursor"
    REACQUIRE_CONTROL = "reacquire_control"


#: The resume order. Tests assert the orchestrator runs exactly this.
RESUME_ORDER: tuple[str, ...] = tuple(step.value for step in ResumeStep)

#: Outcome recorded for a pending command id whose status query proves
#: nothing (404/None, transport failure, or ambiguous envelope).
PENDING_UNKNOWN = "unknown"


class ResumeError(Exception):
    """A resume step failed; control was NOT granted.

    `step` names the failed step; `reason` carries the underlying error.
    Steps after `step` never ran.
    """

    def __init__(self, step: str, reason: str) -> None:
        super().__init__(f"resume failed at {step}: {reason}")
        self.step = step
        self.reason = reason


@dataclass(frozen=True)
class ResumeReport:
    """Completed resume order, with the actual control outcome."""

    steps: tuple[str, ...] = ()
    route: RouteState = field(default_factory=RouteState)
    pending: dict[str, str] = field(default_factory=dict)
    snapshot_cursor: str = ""
    control_granted: bool = False


def restore_minimal_route(stored: str) -> RouteState:
    """Restore the route-only state after death.

    Routes carry opaque identity keys only, never tokens or titles, so a
    stored route string is safe to re-parse. Unknown destinations fall
    back to the default route rather than failing resume.
    """
    return parse_route(stored)


class ResumeOrchestrator:
    """Runs the six resume steps in order with injected I/O.

    - `refresh_identity()`: returns the validated identity record; any
      exception aborts resume (revoked/expired login must surface before
      anything else is trusted).
    - `restore_route(stored)`: maps the stored route string to a
      RouteState (defaults to `restore_minimal_route`).
    - `query_pending(command_id)`: returns the current status doc for one
      outstanding command id, or None when the server knows nothing
      about it (404). Exceptions for one id mark THAT id unknown; they
      do not abort resume (an unknown command must never block control
      return, and is never resent).
    - `fetch_snapshot()`: returns the current snapshot doc; must carry a
      string `cursor` (missing/empty cursor aborts: reconnecting without
      a resume position would silently drop history).
    - `reconnect_cursor(cursor)`: reconnects the metadata stream at the
      snapshot cursor.
    - `reacquire_control()`: explicitly re-acquires the control writer
      and returns its binding description.
    """

    def __init__(
        self,
        *,
        refresh_identity: Callable[[], Awaitable[dict[str, Any]]],
        restore_route: Callable[[str], RouteState] = restore_minimal_route,
        query_pending: Callable[[str], Awaitable[dict[str, Any] | None]],
        fetch_snapshot: Callable[[], Awaitable[dict[str, Any] | None]],
        reconnect_cursor: Callable[[str], Awaitable[None]],
        reacquire_control: Callable[[], Awaitable[str | None]],
    ) -> None:
        self._refresh_identity = refresh_identity
        self._restore_route = restore_route
        self._query_pending = query_pending
        self._fetch_snapshot = fetch_snapshot
        self._reconnect_cursor = reconnect_cursor
        self._reacquire_control = reacquire_control

    async def resume(
        self,
        *,
        stored_route: str,
        outstanding_command_ids: tuple[str, ...] = (),
    ) -> ResumeReport:
        steps: list[str] = []
        try:
            await self._refresh_identity()
        except Exception as exc:
            raise ResumeError(ResumeStep.REFRESH_IDENTITY.value, str(exc)) from exc
        steps.append(ResumeStep.REFRESH_IDENTITY.value)

        try:
            route = self._restore_route(stored_route)
        except Exception as exc:
            raise ResumeError(ResumeStep.RESTORE_ROUTE.value, str(exc)) from exc
        steps.append(ResumeStep.RESTORE_ROUTE.value)

        pending: dict[str, str] = {}
        for command_id in outstanding_command_ids:
            try:
                doc = await self._query_pending(command_id)
            except Exception:
                pending[command_id] = PENDING_UNKNOWN
                continue
            if doc is None:
                pending[command_id] = PENDING_UNKNOWN
            else:
                pending[command_id] = str(doc.get("status", PENDING_UNKNOWN))
        steps.append(ResumeStep.QUERY_PENDING.value)

        try:
            snapshot = await self._fetch_snapshot()
        except Exception as exc:
            raise ResumeError(ResumeStep.FETCH_SNAPSHOT.value, str(exc)) from exc
        if snapshot is None:
            # Validated empty host grants have no metadata cursor or writer to reacquire.
            steps.append(ResumeStep.FETCH_SNAPSHOT.value)
            return ResumeReport(steps=tuple(steps), route=route, pending=pending)
        cursor = snapshot.get("cursor", "")
        if not isinstance(cursor, str) or not cursor:
            raise ResumeError(ResumeStep.FETCH_SNAPSHOT.value, "snapshot has no cursor")
        steps.append(ResumeStep.FETCH_SNAPSHOT.value)

        try:
            await self._reconnect_cursor(cursor)
        except Exception as exc:
            raise ResumeError(ResumeStep.RECONNECT_CURSOR.value, str(exc)) from exc
        steps.append(ResumeStep.RECONNECT_CURSOR.value)

        try:
            control = await self._reacquire_control()
        except Exception as exc:
            raise ResumeError(ResumeStep.REACQUIRE_CONTROL.value, str(exc)) from exc
        steps.append(ResumeStep.REACQUIRE_CONTROL.value)

        return ResumeReport(
            steps=tuple(steps),
            route=route,
            pending=pending,
            snapshot_cursor=cursor,
            control_granted=bool(control),
        )
