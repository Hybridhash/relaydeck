"""Async-safe Flet event adapters.

Flet awaits only coroutine-function handlers, so a sync wrapper must not drop
an async result. Native sync listeners use spawn to observe task failures.
Text payloads take precedence over control props because native property
patches can reach a different reconciled control before event dispatch.
"""

from __future__ import annotations

import asyncio
import inspect
import logging
from collections.abc import Callable
from typing import Any

from relaydeck.client.diagnostics import failure_category, safe_stage

logger = logging.getLogger(__name__)


async def _await_if_needed(result: Any) -> Any:
    if inspect.isawaitable(result):
        return await result
    return result


def click(fn: Callable[..., Any], *args: Any, **kwargs: Any) -> Any:
    """Build an `async def` click handler calling `fn(*args, **kwargs)`.

    The handler qualifies for Flet's awaited branch, so async session
    methods (send, keys, paste, takeover, resize, copy) actually run;
    sync callbacks behave exactly as before.
    """

    async def _on_click(event: Any) -> Any:
        del event
        return await _await_if_needed(fn(*args, **kwargs))

    return _on_click


def event_text(event: Any) -> str:
    """Authoritative text for change/select events (pinned flet 1.0.3).

    Dart sends the new value twice: a property patch plus the event
    payload (textfield/dropdown/navigation sources). The patch lands on
    the session index while dispatch may resolve a different live
    control, so `event.control.value` can be stale (typed composer text
    visibly reset on round-trip). The payload is authoritative —
    including "" (a real clear) — and control props are only a fallback
    for synthetic events without data.
    """
    data = getattr(event, "data", None)
    if isinstance(data, str):
        return data
    control = getattr(event, "control", None)
    value = getattr(control, "value", "") if control is not None else ""
    return value if isinstance(value, str) else ""


def event_index(event: Any) -> int:
    """Authoritative selected index for navigation change events.

    Dart sends the index as the payload (int); same staleness rule as
    `event_text`. Unparseable events fall back to the control prop,
    then to 0 — handlers never crash on a hostile payload.
    """
    data = getattr(event, "data", None)
    if isinstance(data, bool):
        data = None
    if data is None:
        control = getattr(event, "control", None)
        data = getattr(control, "selected_index", 0) if control is not None else 0
    try:
        return int(data)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        control = getattr(event, "control", None)
        fallback = getattr(control, "selected_index", 0) if control is not None else 0
        try:
            return int(fallback)
        except (TypeError, ValueError):
            return 0


def value_change(fn: Callable[[str], Any]) -> Any:
    """Build an `async def` change handler passing the event payload text."""

    async def _on_change(event: Any) -> Any:
        return await _await_if_needed(fn(event_text(event)))

    return _on_change


def selection_change(fn: Callable[[int, int], Any]) -> Any:
    """Build an `async def` selection handler passing UTF-16 offsets.

    TextField `on_selection_change` carries a TextSelectionChangeEvent
    whose base/extent count UTF-16 code units (callers convert with
    `utf16_to_str_offset`); a non-dict payload arrives as a plain
    ControlEvent without `.selection` and is ignored, never crashed.
    """

    async def _on_selection_change(event: Any) -> Any:
        selection = getattr(event, "selection", None)
        base = getattr(selection, "base_offset", None)
        extent = getattr(selection, "extent_offset", None)
        if base is None or extent is None:
            return None
        return await _await_if_needed(fn(base, extent))

    return _on_selection_change


def spawn(coro: Any, *, label: str) -> asyncio.Task[Any]:
    """Schedule `coro` from a sync listener; log failures, never raise.

    Used for extension size/input listeners, which fire from sync Dart
    dispatch where nothing can await: dropping the coroutine would warn
    and lose input, raising would break dispatch. The done callback
    logs a content-free category (cancellation is skipped) and the
    task is returned so tests can await it deterministically.
    """
    task: asyncio.Task[Any] = asyncio.ensure_future(coro)

    def _done(done_task: asyncio.Task[Any]) -> None:
        if done_task.cancelled():
            return
        exc = done_task.exception()
        if exc is not None:
            # Content-free: stage + allowlisted category, never the message,
            # args or traceback (they can embed terminal/user data).
            logger.error(
                "terminal listener failed: stage=%s category=%s",
                safe_stage(label),
                failure_category(exc),
            )

    task.add_done_callback(_done)
    return task


__all__ = ["click", "event_index", "event_text", "selection_change", "spawn", "value_change"]
