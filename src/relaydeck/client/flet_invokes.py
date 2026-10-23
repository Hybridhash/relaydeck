"""Pinned Flet 1.0.3 compatibility for replies from retired native controls."""

from __future__ import annotations

from typing import Any

from flet.controls.base_control import BaseControl
from flet.messaging.session import Session


async def focus_control(target: BaseControl) -> None:
    """Bound native focus and ignore expected timeout or control retirement."""
    try:
        if target.page.session.index.get(target._i) is target:
            await target._invoke_method("focus", timeout=1)
    except (TimeoutError, RuntimeError):
        pass


def install_invoke_result_guard(session: Any) -> None:
    """Drop orphan replies; keep registered-control result handling unchanged."""
    if not isinstance(session, Session) or getattr(session, "_relaydeck_invoke_guard", False):
        return
    receive = session.handle_invoke_method_results

    def handle(control_id: int, call_id: str, result: Any, error: str | None) -> None:
        # A late reply is possible even after the invoke's timeout or the page's unmount.
        if control_id in session.index:
            receive(control_id, call_id, result, error)

    guarded: Any = session
    guarded.handle_invoke_method_results = handle
    guarded._relaydeck_invoke_guard = True
