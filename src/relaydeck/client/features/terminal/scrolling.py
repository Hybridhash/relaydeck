"""Local viewport scrolling and a narrow, generation-fenced PTY scroll route."""

from __future__ import annotations

import asyncio
import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any
from weakref import WeakKeyDictionary, ref

import flet as ft
from relaydeck_terminal.protocol import (
    MAX_SWIPE_PAGES,
    ORIGIN_KEY,
    ORIGIN_MOUSE,
    ORIGIN_SWIPE,
    ORIGIN_TEXT,
    SWIPE_UP,
    ViewState,
    decode_swipe,
    input_cancel_epoch,
)

from relaydeck.client.components.design_system import tokens as console_tokens
from relaydeck.client.features.terminal.events import click, spawn
from relaydeck.client.features.terminal.input_modes import ROUTE_DROPPED, ROUTE_PTY
from relaydeck.client.features.terminal.keys import encode_key

_views: WeakKeyDictionary[Any, tuple[Any, ViewState]] = WeakKeyDictionary()
_DEFAULT = ViewState(alt=False, at_bottom=True, mouse=False)
_sizes: WeakKeyDictionary[Any, tuple[Any, int, int]] = WeakKeyDictionary()
_trackers: WeakKeyDictionary[Any, Any] = WeakKeyDictionary()
MAX_ALT_PAGES = 50
MAX_ALT_WHEEL_STEPS = MAX_ALT_PAGES * 24
ALT_SELECTION_HINT = (
    "Showing this screen only. Scroll with ▲ (in control) to reach older output, then Select again."
)


@dataclass
class AltScrollPosition:
    """Bounded device navigation estimate; the alternate app owns its viewport."""

    page_lines: int = 23
    lines: int = 0
    wheel_up: bytes | None = None
    wheel_down: bytes | None = None

    def move(self, amount: int) -> None:
        self.lines = max(0, min(MAX_ALT_PAGES * self.page_lines, self.lines + amount))

    def latest(self) -> tuple[bytes, ...]:
        # A partial page still needs PageDown: arrows edit agent prompts.
        pages = min(MAX_ALT_PAGES, (self.lines + self.page_lines - 1) // self.page_lines)
        return (b"\x1b[6~",) * pages

    def note_wheel(self, data: bytes) -> None:
        """Reuse the observed native mouse encoding, never guess a mode bit."""
        text = data.decode("utf-8")
        if text.startswith("\x1b[M"):
            suffix = text[4:]
            self.wheel_up = ("\x1b[M`" + suffix).encode()
            self.wheel_down = ("\x1b[Ma" + suffix).encode()
        else:
            _, x, y = re.findall(r"\d+", text)
            prefix = "\x1b[<" if text.startswith("\x1b[<") else "\x1b["
            up, down = (64, 65) if prefix.endswith("<") else (96, 97)
            self.wheel_up = f"{prefix}{up};{x};{y}M".encode()
            self.wheel_down = f"{prefix}{down};{x};{y}M".encode()


@dataclass
class _Tracker:
    owner: Any
    position: AltScrollPosition
    current: Callable[[], bool]
    changed: Callable[[], None]


def note_view_size(term: Any, cols: int, rows: int) -> None:
    _sizes[term] = (getattr(term, "_reset_count", None), cols, rows)


def _tracker(session: Any) -> _Tracker | None:
    if session.current_term is None:
        return None
    pointer = _trackers.get(session.current_term)
    tracker = pointer() if pointer is not None else None
    return (
        tracker
        if tracker is not None and tracker.owner() is session and tracker.current()
        else None
    )


def reset_alt_scroll(session: Any) -> None:
    tracker = _tracker(session)
    if tracker is not None and tracker.position.lines:
        tracker.position.lines = 0
        tracker.changed()


def record_scroll_input(session: Any, origin: int, data: bytes, *, scroll: bool = False) -> None:
    """Record only successfully sent bytes from the authoritative writer path."""
    tracker = _tracker(session)
    if tracker is None or not view_state(session.current_term).alt:
        return
    if (origin == ORIGIN_TEXT and data) or data in (b"\r", b"\n", b"\r\n"):
        reset_alt_scroll(session)
        return
    position = tracker.position
    if data == b"\x1b[5~":
        position.move(position.page_lines)
    elif data == b"\x1b[6~":
        position.move(-position.page_lines)
    elif origin == ORIGIN_KEY and not scroll:
        reset_alt_scroll(session)
        return
    elif scroll:
        if data in (b"\x1b[A", b"\x1bOA"):
            position.move(1)
        elif data in (b"\x1b[B", b"\x1bOB"):
            position.move(-1)
        elif origin == ORIGIN_MOUSE and is_scroll_input(
            origin, data, view_state(session.current_term)
        ):
            position.note_wheel(data)
            # The whitelist above accepts only one vertical wheel event.
            text = data.decode("utf-8")
            if text.startswith("\x1b[<"):
                button = int(text[3:].split(";", 1)[0])
            elif text.startswith("\x1b[M"):
                button = ord(text[3]) - 32
            else:
                button = int(text[2:].split(";", 1)[0]) - 32
            position.move(1 if button & ~28 == 64 else -1)
    tracker.changed()


def view_state(term: Any) -> ViewState:
    if term is None:
        return _DEFAULT
    revision, state = _views.get(term, (None, _DEFAULT))
    return (
        state if term is not None and revision == getattr(term, "_reset_count", None) else _DEFAULT
    )


def swipe_pages(count: int, page_lines: int) -> int:
    """Swipe line count to bounded whole pages: at least one (every real
    gesture moves), at most MAX_SWIPE_PAGES per packet."""
    return min(MAX_SWIPE_PAGES, max(1, round(count / max(1, page_lines))))


def last_lines(text: str, count: int) -> str:
    """LF-delimited logical lines, preserving CRLF and a final line terminator."""
    lines = text.split("\n")
    length = len(lines) - int(text.endswith("\n"))
    start = sum(len(line) + 1 for line in lines[: max(0, length - count)])
    return text[start:]


def is_scroll_input(origin: int, data: bytes, state: ViewState) -> bool:
    """Accept only xterm's vertical wheel frames or alternate-buffer arrow fallback.

    Mouse clicks, motion, releases, horizontal wheels and concatenated packets
    remain rejected. This is not a general mouse-input authorization.
    """
    if not state.alt:
        return False
    if origin == ORIGIN_KEY:
        return data in (b"\x1b[A", b"\x1b[B", b"\x1bOA", b"\x1bOB")
    if origin != ORIGIN_MOUSE or not state.mouse:
        return False
    try:
        text = data.decode("utf-8", errors="strict")
    except UnicodeDecodeError:
        return False
    sgr = re.fullmatch(r"\x1b\[<(\d+);(\d+);(\d+)M", text)
    urxvt = re.fullmatch(r"\x1b\[(\d+);(\d+);(\d+)M", text)
    if sgr or urxvt:
        match = sgr or urxvt
        assert match is not None  # noqa: S101
        button, x, y = map(int, match.groups())
        if urxvt:
            button -= 32
    elif text.startswith("\x1b[M") and len(text) == 6:
        button, x, y = (ord(char) - 32 for char in text[3:])
    else:
        return False
    return button & ~28 in (64, 65) and 0 < x <= 65535 and 0 < y <= 65535


def use_open_at_end(session: Any) -> None:
    """A terminal brought to the front shows its newest output.

    The engine stops following the end once a touch moves its view, and a resync replaces the
    screen without following again, so a terminal could reopen partway up its history. A fresh
    engine follows the end already. The jump is queued in order with the output."""
    term = session.current_term
    foreground = bool(getattr(session, "foreground", False))
    # The first view report of an engine is what makes the jump possible; without it in the
    # dependencies a cold open that reported late would never be brought to the end. A resync is
    # not a trigger: its baseline report can be suppressed when nothing changed, and the next
    # report would then be a deliberate scroll that must not be undone.
    reported = term is not None and term in _views

    def jump() -> None:
        if foreground and term is not None and reported and not view_state(term).alt:
            term.scroll_viewport(None)
            session.jump_to_bottom()

    ft.use_effect(
        jump,
        dependencies=[id(term) if term is not None else 0, foreground, reported],
    )


def scroll_away(state: ViewState | None) -> bool:
    """True when a "Jump to latest" is useful: scrolled back, or a full-screen app is showing."""
    return state is not None and (state.alt or not state.at_bottom)


def use_terminal_scroll(
    session: Any, send: Callable[..., Awaitable[str]], *, touch: bool = False
) -> dict[str, Any]:
    """One boundary for buttons and native touch/wheel across all width classes.

    `touch` marks pointer-coarse clients (iPhone/iPad/Android): they never
    emit a native wheel, so an alt+mouse app must not hide its scroll
    buttons forever waiting for an encoder that never arrives. Unknown
    encodings fall back to PageUp/PageDown; a learned wheel keeps
    precedence everywhere."""
    _tick, set_tick = ft.use_state(0)
    term = session.current_term
    state = view_state(term)
    controller = session.controller
    size = _sizes.get(term, (None, 80, 24)) if term is not None else (None, 80, 24)
    binding = (
        term,
        getattr(term, "viewer_generation", None),
        getattr(term, "_reset_count", None),
        session._view_token,
        controller,
        controller.generation if controller is not None else None,
        session.input_token,
        session.interaction_generation,
        session.term_state.epoch,
        state.alt,
        size,
        session.term_state.lease_mode,
        session.keys_enabled,
        state.mouse,
    )
    tracking: Any = ft.use_ref(None)
    transaction: Any = ft.use_ref(lambda: asyncio.Lock())
    if tracking.current is None or tracking.current[0] != binding:
        tracking.current = (binding, AltScrollPosition(page_lines=max(1, min(1024, size[2]) - 1)))
    position = tracking.current[1]

    def live() -> bool:
        current = session.current_term
        return (
            term is not None
            and session.foreground
            and not session.paused
            and not session.disposed
            and not session.term_state.selection.open
            and current is term
            and getattr(current, "viewer_generation", None) == binding[1]
            and getattr(current, "_reset_count", None) == binding[2]
            and session._view_token == binding[3]
            and session.controller is controller
            and (controller is None or controller.generation == binding[5])
            and session.input_token == binding[6]
            and session.interaction_generation == binding[7]
            and session.term_state.epoch == binding[8]
            and view_state(term).alt == state.alt
            and view_state(term).mouse == state.mouse
            and tracking.current[0] == binding
            and tracking.current[1] is position
        )

    def allowed() -> bool:
        return live() and (
            not state.alt or (session.keys_enabled and session.term_state.lease_mode == "control")
        )

    def buttons_allowed() -> bool:
        # VIEW_REPORT carries mouse on/off, not its encoding. Wait for one
        # accepted native wheel to learn the encoder; do not invent SGR mode.
        if not allowed():
            return False
        if not state.alt or not state.mouse or position.wheel_down is not None:
            return True
        # Touch never produces that wheel; offer the safe
        # PageUp/PageDown default rather than hiding the buttons.
        return touch

    tracker = _Tracker(ref(session), position, allowed, lambda: set_tick(lambda tick: tick + 1))
    # Only weak references escape the component; retained native objects must
    # not retain an unmounted hook/session via a global callback closure.
    tracker_ref: Any = ft.use_ref(None)
    tracker_ref.current = tracker
    if term is not None:
        _trackers[term] = ref(tracker)

    def report(reporter: Any, value: ViewState) -> None:
        current = session.current_term
        if current is reporter:
            _views[current] = (getattr(current, "_reset_count", None), value)
            set_tick(lambda tick: tick + 1)

    async def scroll(pages: int | None) -> None:
        async with transaction.current:
            if not buttons_allowed() or pages not in (-1, 1, None):
                return
            if state.alt:
                origin = ORIGIN_KEY
                if state.mouse:
                    payload = position.wheel_up if pages == -1 else position.wheel_down
                    if payload is not None:
                        origin = ORIGIN_MOUSE
                        steps = min(
                            MAX_ALT_WHEEL_STEPS,
                            position.page_lines
                            if pages is not None
                            else position.lines or MAX_ALT_WHEEL_STEPS,
                        )
                        packets = (payload,) * steps
                    elif touch:
                        # Unknown encoder on touch: the safe PageUp/PageDown
                        # default (commit_bytes path, no invented SGR mode).
                        origin = ORIGIN_KEY
                        if pages is not None:
                            key_payload = encode_key("page-up" if pages < 0 else "page-down")
                            packets = (key_payload,) if key_payload is not None else ()
                        else:
                            packets = position.latest() or (b"\x1b[6~",) * MAX_ALT_PAGES
                    else:
                        packets = ()
                elif pages is not None:
                    payload = encode_key("page-up" if pages < 0 else "page-down")
                    packets = (payload,) if payload is not None else ()
                else:
                    packets = position.latest() or (b"\x1b[6~",) * MAX_ALT_PAGES
                if packets:
                    result = await send(
                        origin, packets, terminal_scroll=True, can_input=buttons_allowed
                    )
                    if result == ROUTE_PTY and pages is None and buttons_allowed():
                        # The shared writer records each sent packet. Keep a
                        # capped wheel jump's unsent remainder instead of
                        # pretending to have reached an app-owned bottom.
                        tracker.changed()
            else:
                term.scroll_viewport(pages)
                if pages is None:
                    session.jump_to_bottom()

    async def swipe(data: bytes) -> str:
        # Swipe gestures carry direction and count, never PTY bytes. Translate to bounded
        # PageUp/PageDown; malformed, out-of-mode or fenced gestures drop here, never become typed
        # text.
        try:
            direction, count = decode_swipe(data)
        except ValueError:
            return ROUTE_DROPPED
        current = view_state(term)
        if not current.alt or current.mouse:
            return ROUTE_DROPPED
        payload = encode_key("page-up" if direction == SWIPE_UP else "page-down")
        if payload is None:
            return ROUTE_DROPPED
        packets = (payload,) * swipe_pages(count, position.page_lines)
        async with transaction.current:
            return await send(ORIGIN_KEY, packets, terminal_scroll=True, can_input=allowed)

    async def native(origin: int, data: bytes) -> str | None:
        if origin == ORIGIN_SWIPE:
            return await swipe(data)
        if not is_scroll_input(origin, data, state):
            return None
        if origin == ORIGIN_MOUSE and getattr(term, "wheel_blocked", False):
            # A server-side key/paste landed first: this report left the
            # widget before it saw the cancel, so it is stale. Explicit
            # arrow keys are fresh input and still flow.
            return ROUTE_DROPPED
        async with transaction.current:
            stamped = input_cancel_epoch.get()
            if (
                origin == ORIGIN_MOUSE
                and stamped >= 0
                and stamped != getattr(term, "cancel_epoch", stamped)
            ):
                # Accepted before a cancel that was issued since: the
                # report predates the viewer handling the cancel, so
                # it is stale even though the ack already landed.
                return ROUTE_DROPPED
            return await send(origin, data, terminal_scroll=True, can_input=allowed)

    return dict(
        scroll_state=ViewState(
            state.alt,
            (position.lines == 0 and state.at_bottom) if state.alt else state.at_bottom,
            state.mouse,
        ),
        scroll_binding=binding,
        on_view_report=report,
        on_scroll=scroll,
        can_scroll=buttons_allowed,
        scroll_input_enabled=state.alt and allowed(),
        native_scroll=native,
    )


@ft.component
def ScrollButtons(
    *,
    visible: bool,
    state: ViewState,
    binding: Any,
    on_scroll: Callable[[int | None], Awaitable[None]] | None,
    can_scroll: Callable[[], bool] | None,
    on_keyboard: Callable[[], Any] | None = None,
    keyboard_shown: bool = False,
    jump_only: bool = False,
    on_tools: Callable[[], Any] | None = None,
    tools_open: bool = False,
) -> Any:
    """A narrow, always-mounted Stack sibling; no wrapper around the engine.

    `jump_only` keeps just "Jump to latest" (mouse clients scroll with the wheel).

    `on_keyboard` adds the keyboard toggle below the scroll buttons
    (touch devices in control only). `on_tools` adds the phone's tools button instead."""
    current = ft.use_ref((binding, visible))
    current.current = (binding, visible)
    holding: Any = ft.use_ref(None)
    alive = ft.use_ref(True)

    def cancel(_event: Any = None) -> None:
        task = holding.current
        holding.current = None
        if task is not None:
            task.cancel()

    def cleanup() -> None:
        alive.current = False
        cancel()

    ft.use_effect(lambda: None, dependencies=[], cleanup=cleanup)
    ft.use_effect(lambda: None, dependencies=[binding, visible], cleanup=cancel)

    def live() -> bool:
        return (
            bool(alive.current)
            and visible
            and current.current == (binding, visible)
            and on_scroll is not None
            and can_scroll is not None
            and can_scroll()
        )

    async def once(pages: int | None) -> None:
        if live() and on_scroll is not None:
            await on_scroll(pages)

    async def repeat(pages: int) -> None:
        while live():
            await once(pages)
            await asyncio.sleep(0.120)

    def start(pages: int) -> None:
        cancel()
        if live():
            holding.current = spawn(repeat(pages), label="terminal-scroll-hold")

    def button(key: str, icon: Any, label: str, pages: int | None) -> Any:
        # A full-screen app owns its own scroll position and can redraw scrolled back (e.g. on
        # reattach) without telling us, so "Jump to latest" stays offered there whenever it works.
        if pages is not None:
            show = not jump_only and (not state.alt or live())
        else:
            show = (not state.at_bottom or state.alt) and (not state.alt or live())
        return ft.Semantics(
            key=f"{key}-semantics",
            label=label if live() else f"{label}, unavailable",
            button=True,
            exclude_semantics=True,
            visible=show,
            on_tap=click(lambda: once(pages)) if live() else None,
            content=ft.GestureDetector(
                key=f"{key}-hold",
                exclude_from_semantics=True,
                on_tap=click(lambda: once(pages)),
                on_long_press_start=click(lambda: start(pages)) if pages is not None else None,
                on_long_press_end=cancel,
                on_long_press_up=cancel,
                on_long_press_cancel=cancel,
                # The transparent painted box stops taps reaching the engine.
                # Ignore only the inner button's pointer recognizers: pinned
                # Flet always gives it a competing long-press recognizer.
                # Its real keyboard focus/actions remain available.
                content=ft.Container(
                    width=32,
                    height=32,
                    bgcolor=ft.Colors.TRANSPARENT,
                    content=ft.Container(
                        ignore_interactions=True,
                        content=ft.IconButton(
                            key=key,
                            icon=icon,
                            icon_size=20,
                            width=32,
                            height=32,
                            padding=0,
                            style=ft.ButtonStyle(
                                bgcolor=ft.Colors.with_opacity(
                                    0.35, console_tokens.SCROLL_THUMB_BG
                                ),
                                color=console_tokens.ON_PRIMARY,
                                shape=ft.CircleBorder(),
                            ),
                            tooltip=None,
                            disabled=not live(),
                            visible=show,
                            on_click=click(lambda: once(pages)),
                        ),
                    ),
                ),
            ),
        )

    # Flet Containers hit-test their whole rectangle and cover the native keyboard button.
    return ft.Column(
        key="terminal-scroll-overlay",
        visible=visible and not tools_open,
        right=8,
        top=0,
        bottom=0,
        width=32,
        alignment=ft.MainAxisAlignment.CENTER,
        horizontal_alignment=ft.CrossAxisAlignment.CENTER,
        tight=False,
        spacing=0,
        controls=[
            ft.Column(
                key="terminal-scroll-buttons",
                tight=True,
                spacing=6,
                controls=[
                    button("terminal-scroll-up", ft.Icons.KEYBOARD_ARROW_UP, "Scroll up", -1),
                    button("terminal-scroll-down", ft.Icons.KEYBOARD_ARROW_DOWN, "Scroll down", 1),
                    button(
                        "terminal-scroll-latest",
                        ft.Icons.VERTICAL_ALIGN_BOTTOM,
                        "Jump to latest",
                        None,
                    ),
                    *(
                        [
                            ft.IconButton(
                                key="terminal-keyboard-button",
                                icon=ft.Icons.KEYBOARD_HIDE
                                if keyboard_shown
                                else ft.Icons.KEYBOARD,
                                icon_size=20,
                                width=32,
                                height=32,
                                padding=0,
                                style=ft.ButtonStyle(
                                    bgcolor=ft.Colors.with_opacity(
                                        0.35, console_tokens.SCROLL_THUMB_BG
                                    ),
                                    color=console_tokens.ON_PRIMARY,
                                    shape=ft.CircleBorder(),
                                ),
                                tooltip="Hide keyboard" if keyboard_shown else "Show keyboard",
                                on_click=click(on_keyboard),
                            )
                        ]
                        if on_keyboard is not None
                        else []
                    ),
                    *(
                        [
                            ft.Semantics(
                                label="Keys and actions",
                                button=True,
                                expanded=tools_open,
                                content=ft.IconButton(
                                    key="terminal-tools-button",
                                    icon=ft.Icons.KEYBOARD_COMMAND_KEY,
                                    icon_size=18,
                                    width=32,
                                    height=32,
                                    padding=0,
                                    style=ft.ButtonStyle(
                                        bgcolor=console_tokens.PRIMARY_FILL,
                                        color=console_tokens.ON_PRIMARY,
                                        shape=ft.CircleBorder(),
                                    ),
                                    on_click=click(on_tools),
                                ),
                            )
                        ]
                        if on_tools is not None
                        else []
                    ),
                ],
            )
        ],
    )
