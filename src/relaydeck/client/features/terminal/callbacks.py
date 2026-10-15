"""Generation-bound actions for retained views; output bindings stay independent."""

from __future__ import annotations

import inspect
from collections.abc import Callable
from typing import Any

from relaydeck.client.features.sync.transport import TransportError
from relaydeck.client.features.terminal.diagnostics import diagnostics_logger


def foreground_action(session: Any, action: Callable[..., Any]) -> Callable[..., Any]:
    generation = session.interaction_generation

    def live() -> bool:
        return (
            session.foreground
            and not getattr(session, "paused", False)
            and not session.disposed
            and generation == session.interaction_generation
        )

    if inspect.iscoroutinefunction(action):

        async def call_async(*args: Any, **kwargs: Any) -> Any:
            if not live():
                return "ignored"
            try:
                return await action(*args, **kwargs)
            except TransportError as exc:
                # Never let a transport failure reach Flet's page error: the action's own
                # session state carries the inline error and its retry.
                diagnostics_logger().info(
                    "action_transport_error type=%s code=%s", type(exc).__name__, exc.code
                )
                return "failed"

        return call_async

    def call(*args: Any, **kwargs: Any) -> Any:
        if not live():
            return None
        try:
            return action(*args, **kwargs)
        except TransportError as exc:
            diagnostics_logger().info(
                "action_transport_error type=%s code=%s", type(exc).__name__, exc.code
            )
            return None

    return call


def view_actions(session: Any, props: dict[str, Any]) -> dict[str, Any]:
    return {
        key: foreground_action(session, value)
        if key.startswith("on_")
        and key not in ("on_terminal", "on_mode_report")
        and callable(value)
        else value
        for key, value in props.items()
    }
