"""Optional haptics that never interrupt Send or Copy.

One service per page uses native feedback or the browser Vibration API where
available. Send taps on accepted intent; Copy taps after the write. Missing
platform support and service failures leave the action result unchanged.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from typing import Any, Protocol

import flet as ft
from flet.controls.context import context

LOG = logging.getLogger("relaydeck.client.haptics")
_SERVICE_ATTR = "_relaydeck_haptic_feedback"
# Strong references to fire-and-forget tap tasks, so they are not collected mid-flight.
_PENDING: set[asyncio.Task[None]] = set()


class LightImpact(Protocol):
    async def light_impact(self) -> Any: ...


def page_haptic_service() -> ft.HapticFeedback | None:
    """This page's haptic service, created during render. None without a page or support."""
    try:
        page = context.page
    except RuntimeError:
        return None
    service = getattr(page, _SERVICE_ATTR, None)
    if service is None:
        try:
            service = ft.HapticFeedback()
        except Exception:  # noqa: BLE001 - no service support: no haptics
            LOG.debug("haptic service unavailable", exc_info=True)
            return None
        setattr(page, _SERVICE_ATTR, service)
    return service


def tap(service: LightImpact | None, enabled: bool) -> None:
    """One light tap, not awaited by the UI path. Silent when disabled or unsupported."""
    if not enabled or service is None:
        return
    try:
        task = asyncio.get_running_loop().create_task(_light(service))
    except RuntimeError:
        LOG.debug("haptic tap without a running loop")
        return
    _PENDING.add(task)
    task.add_done_callback(_PENDING.discard)


async def _light(service: LightImpact) -> None:
    try:
        await service.light_impact()
    except Exception:  # noqa: BLE001 - unsupported platform or refused: silent
        LOG.debug("haptic tap failed", exc_info=True)


def on_send(
    send: Callable[[], Awaitable[Any]], enabled: bool, ready: Callable[[], bool]
) -> Callable[[], Awaitable[Any]]:
    """Tap at the tap, before the send starts, when `ready()` says the send will go.

    Delivery can take tens of seconds over a relay, so the tap cannot wait for the result.
    `ready` is read at call time (the composer's live `can_send`, not sending, no review);
    an ignored, disabled or empty send is not ready and stays silent. Call during render:
    the page's service is resolved here, once per wrapper.
    """
    service = page_haptic_service()

    async def sent() -> Any:
        if ready():
            tap(service, enabled)
        return await send()

    return sent


def on_copy(
    copy: Callable[[int, int], Awaitable[Any]], enabled: bool
) -> Callable[[int, int], Awaitable[Any]]:
    """Tap after a selection copy returned; a failed copy raises and is not tapped.
    Call during render, like `on_send`."""
    service = page_haptic_service()

    async def copied(anchor: int, focus: int) -> Any:
        result = await copy(anchor, focus)
        tap(service, enabled)
        return result

    return copied


def on_phone_copy(
    copy: Callable[[str], Awaitable[str | None]], enabled: bool
) -> Callable[[str], Awaitable[str | None]]:
    """Tap only when the phone copy wrote something (it returns None when it did not act).
    Call during render, like `on_send`."""
    service = page_haptic_service()

    async def copied(mode: str) -> str | None:
        result = await copy(mode)
        if result is not None:
            tap(service, enabled)
        return result

    return copied
