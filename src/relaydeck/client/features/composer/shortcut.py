"""Ctrl/Cmd+Enter sends the agent draft through the same entry point as the Send button.

Plain Enter and Shift+Enter keep inserting a newline. The page reports a key press (with the
modifier flags) before the text field reacts, and the field still inserts a newline for it, so
the one newline that press adds is swallowed instead of reaching the draft.
"""

from __future__ import annotations

import inspect
import time
from collections.abc import Callable
from typing import Any

import flet as ft
from flet.controls.context import context

from relaydeck.client.features.terminal.events import spawn


async def _await(awaitable: Any) -> None:
    await awaitable


_REGISTRY_ATTR = "_relaydeck_send_shortcuts"
_ENTER_KEYS = ("Enter", "Numpad Enter")
_SWALLOW_WINDOW_S = 1.0


def platform_label() -> str:
    """Hint for the Send tooltip: the Command key on Apple platforms, otherwise Ctrl."""
    try:
        platform = str(getattr(context.page, "platform", "") or "").lower()
    except RuntimeError:
        platform = ""
    return "⌘+Enter" if "macos" in platform or "ios" in platform else "Ctrl+Enter"


def inserted_one_newline(before: str, after: str) -> bool:
    """True when `after` is `before` with exactly one newline added somewhere."""
    if len(after) != len(before) + 1:
        return False
    cut = 0
    while cut < len(before) and before[cut] == after[cut]:
        cut += 1
    return after[cut] == "\n" and after[cut + 1 :] == before[cut:]


def _dispatch(event: Any) -> None:
    if getattr(event, "key", "") not in _ENTER_KEYS:
        return
    if not (getattr(event, "ctrl", False) or getattr(event, "meta", False)):
        return
    if getattr(event, "shift", False) or getattr(event, "alt", False):
        return
    page = context.page
    for entry in tuple(getattr(page, _REGISTRY_ATTR, ())):
        entry()


class SendShortcut:
    def __init__(self) -> None:
        self.focused = False
        self.draft = ""
        self.can_send = False
        self.send: Callable[[], Any] = lambda: None
        self._pending: tuple[str, float] | None = None

    def focus(self, focused: bool) -> None:
        self.focused = focused

    def fire(self) -> None:
        if not self.focused or not self.can_send:
            return
        self._pending = (self.draft, time.monotonic())
        result = self.send()
        if inspect.isawaitable(result):
            spawn(_await(result), label="Send with shortcut")

    def swallows(self, text: str) -> bool:
        """True for the change that is only the newline this shortcut's Enter inserted."""
        pending, self._pending = self._pending, None
        if pending is None or time.monotonic() - pending[1] > _SWALLOW_WINDOW_S:
            return False
        return inserted_one_newline(pending[0], text)


def use_send_shortcut(
    *, draft: str, can_send: bool, send: Callable[[], Any]
) -> tuple[SendShortcut, str]:
    """Register this composer for the shortcut; returns its controller and the tooltip hint.

    Only the composer whose draft field has focus acts, so parked composers stay silent.
    """
    holder: Any = ft.use_ref(SendShortcut)
    shortcut: SendShortcut = holder.current
    shortcut.draft, shortcut.can_send, shortcut.send = draft, can_send, send

    detach_ref: Any = ft.use_ref(None)

    def attach() -> None:
        try:
            page = context.page
        except RuntimeError:
            return
        registry: list[Callable[[], None]] | None = getattr(page, _REGISTRY_ATTR, None)
        if registry is None:
            registry = []
            setattr(page, _REGISTRY_ATTR, registry)
        registry.append(shortcut.fire)
        if page.on_keyboard_event is not _dispatch:
            page.on_keyboard_event = _dispatch

        def detach() -> None:
            if shortcut.fire in registry:
                registry.remove(shortcut.fire)
            if not registry and page.on_keyboard_event is _dispatch:
                page.on_keyboard_event = None

        detach_ref.current = detach

    def detach_all() -> None:
        if detach_ref.current is not None:
            detach_ref.current()
            detach_ref.current = None

    ft.use_effect(attach, dependencies=[], cleanup=detach_all)
    return shortcut, platform_label()
