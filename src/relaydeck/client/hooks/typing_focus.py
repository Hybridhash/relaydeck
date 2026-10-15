"""Typing-mode focus hook of `Root`.

Plain functions called from `Root` at the position the inline code used to occupy, so hook
order and the control tree are unchanged. Each takes only what it reads (keyword-only) and
returns the names the rest of `Root` still uses.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from typing import Any

import flet as ft

from relaydeck.client.features.terminal.diagnostics import diagnostics_logger
from relaydeck.client.features.terminal.layout_budget import typing_split_active
from relaydeck.client.hooks.support import TYPING_BLUR_DEBOUNCE_S


def use_typing_focus(
    *, sticky: bool = False, viewport: tuple[float, float] = (0.0, 0.0), route: tuple[Any, ...] = ()
) -> tuple[Any, ...]:
    """The agent composer focus, lifted for the Console column and detail layout.

    `sticky` (touch devices) makes typing mode outlive the field's
    focus. A blur then keeps the split (the terminal can be scrolled and used while
    composing) and only an explicit exit (`use_typing_exit`) leaves it. Without
    `sticky` the blur is debounced and exits, as before (desktop never splits)."""
    # the agent composer's focus, lifted so the Console column
    # and the detail layout can follow it. Blur is debounced because moving
    # the box into the typing column remounts the field (it refocuses).
    typing_focus, set_typing_focus = ft.use_state(False)
    # the real field focus (the software keyboard is up), kept apart
    # from typing mode, which stays on while the field is blurred.
    field_focused, set_field_focused = ft.use_state(False)

    typing_blur_task: Any = ft.use_ref(None)

    def cancel_typing_blur() -> None:
        pending = typing_blur_task.current
        typing_blur_task.current = None
        if pending is not None:
            pending.cancel()

    def on_composer_focus(focused: bool) -> None:
        diagnostics_logger().info("typing_focus focused=%s", focused)
        set_field_focused(focused)
        cancel_typing_blur()
        if focused:
            set_typing_focus(True)
            return
        if sticky:
            return

        async def leave() -> None:
            await asyncio.sleep(TYPING_BLUR_DEBOUNCE_S)
            typing_blur_task.current = None
            set_typing_focus(False)

        try:
            typing_blur_task.current = asyncio.create_task(leave())
        except RuntimeError:
            set_typing_focus(False)

    def keep_typing_mode() -> None:
        # a column control keeps typing mode. Field focus is untouched:
        # it changes only from the TextField's own on_focus/on_blur.
        cancel_typing_blur()
        set_typing_focus(True)

    exit_typing = use_typing_exit(
        typing_focus=typing_focus,
        set_typing_focus=set_typing_focus,
        cancel_typing_blur=cancel_typing_blur,
        sticky=sticky,
        portrait=sticky and viewport[0] <= viewport[1],
        route=route,
    )
    return (
        cancel_typing_blur,
        on_composer_focus,
        set_typing_focus,
        typing_focus,
        exit_typing,
        field_focused,
        keep_typing_mode,
    )


TYPING_EXIT_REASONS = ("send", "done", "route", "portrait", "lost")


def use_typing_exit(
    *,
    typing_focus: bool,
    set_typing_focus: Any,
    cancel_typing_blur: Any,
    sticky: bool,
    portrait: bool,
    route: tuple[str, str],
) -> Callable[[str], None]:
    """Exit typing mode with a logged reason.

    Returns `exit(reason)` for the Send and Done controls. Also exits by effect on
    route/agent change and, on touch devices, on rotation to portrait."""
    last_route: Any = ft.use_ref(route)
    typing_now: Any = ft.use_ref(typing_focus)
    typing_now.current = typing_focus

    def exit_typing(reason: str) -> None:
        if reason not in TYPING_EXIT_REASONS:
            raise ValueError(f"unknown typing exit reason: {reason}")
        if not typing_now.current:
            return
        typing_now.current = False
        cancel_typing_blur()
        diagnostics_logger().info("typing_exit reason=%s", reason)
        set_typing_focus(False)

    def leave_on_route_change() -> None:
        if last_route.current != route:
            last_route.current = route
            exit_typing("route")

    ft.use_effect(leave_on_route_change, dependencies=[route])

    def leave_on_portrait() -> None:
        if sticky and portrait:
            exit_typing("portrait")

    ft.use_effect(leave_on_portrait, dependencies=[sticky and portrait])
    return exit_typing


def use_typing_split(
    *,
    platform: str,
    touch: bool,
    viewport: tuple[float, float],
    focused: bool,
    entry: Any,
) -> bool:
    """Whether the typing split is active: the agent's composer is focused in landscape on
    a touch device (`typing_split_active`), and one `typing_split_inputs` line is logged
    whenever any decision input changes.

    Only enum-like names, booleans and sizes; never ids, draft text or agent names."""
    width, height = viewport
    kind = entry.kind if entry is not None else "none"
    phase = getattr(getattr(entry, "session", None), "phase", "") or ""
    split = (
        entry is not None
        and kind == "agent"
        and typing_split_active(
            focused=focused, viewport_width=width, viewport_height=height, touch=touch
        )
    )
    fields = (
        platform,
        touch,
        round(width),
        round(height),
        width > height,
        focused,
        kind,
        phase,
        split,
        touch,
    )

    def log() -> None:
        diagnostics_logger().info(
            "typing_split_inputs platform=%s touch=%s width=%s height=%s landscape=%s "
            "focused=%s kind=%s phase=%s split=%s sticky=%s",
            *fields,
        )

    ft.use_effect(log, dependencies=[fields])
    return split


def log_touch_platform(platform: Any, touch: bool) -> str:
    """Startup decision line (once per session); returns the platform name for Root."""
    name = getattr(platform, "value", None) or str(platform)
    diagnostics_logger().info("touch_platform platform=%s touch=%s", name, touch)
    return str(name)
