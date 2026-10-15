"""Declarative host for a retained terminal renderer.

The renderer keeps a stable key across renders. Recovery notices, selection
and scroll controls remain siblings so they cannot replace the terminal."""

from __future__ import annotations

import inspect
from collections.abc import Callable
from typing import Any

import flet as ft
from relaydeck_terminal.protocol import ViewState
from relaydeck_terminal.terminal import RelayDeckTerminal, TerminalOverflowError

from relaydeck.client.components.design_system.terminal_window import KeyRow, StatusLine
from relaydeck.client.features.a11y.announcer import LiveRegion
from relaydeck.client.features.terminal.connecting_dots import ConnectingDots
from relaydeck.client.features.terminal.diagnostics import diagnostics_logger
from relaydeck.client.features.terminal.events import click, spawn
from relaydeck.client.features.terminal.models import TerminalViewState
from relaydeck.client.features.terminal.scrolling import (
    ScrollButtons,
    note_view_size,
    scroll_away,
)
from relaydeck.client.features.terminal.selection_sheet import SelectTextSheet
from relaydeck.client.theme import BANNER_TONES, MIN_TOUCH, SURFACE_RADIUS, palette

SURFACE_KEY = "terminal-surface"
NOTICE_KEY = "terminal-notice"
NOTICE_TEXT_KEY = "terminal-notice-text"
JUMP_KEY = "scroll-jump"
STATUS_KEY = "terminal-status"
SELECT_BUTTON_KEY = "terminal-select-text"
AREA_KEY = "terminal-area"
TOOLBAR_KEY = "terminal-toolbar"


def _frame_border(tokens: dict[str, str]) -> ft.Border:
    """Reference `.terminal-area` 1px line border (theme-selected)."""
    side = ft.BorderSide(width=1, color=tokens["line"])
    return ft.Border(left=side, top=side, right=side, bottom=side)


def deliver_output(term: RelayDeckTerminal, data: bytes) -> bool:
    """Write stream bytes to the surface. False means the viewer queue
    overflowed: the caller must drive reset + reattach with notice
    (controller.on_viewer_overflow), never silently continue."""
    try:
        term.write(data)
    except TerminalOverflowError:
        return False
    return True


def _noop() -> None:
    pass


def _noop_range(anchor: int, focus: int) -> None:
    del anchor, focus


def _noop_key(name: str) -> None:
    del name


def status_line(state: TerminalViewState) -> str:
    lease = "no lease" if state.lease_mode == "none" else state.lease_mode
    parts = [state.status, lease, f"{state.epoch}@{state.applied_offset}"]
    if not state.scroll.stick_to_bottom:
        parts.append(f"{state.scroll.unseen_bytes} B behind")
    return " · ".join(parts)


def status_summary(state: TerminalViewState, *, attached: bool = True) -> str:
    """Viewport status line text: connection and mode only.

    The byte offset stays in the tooltip (`status_line`); it changes on
    every chunk and is diagnostic, not something to read at a glance."""
    if not attached:
        return "not attached · read only"
    mode = {"control": "control", "view": "read only"}.get(state.lease_mode, "no lease")
    parts = [state.status, mode]
    if not state.scroll.stick_to_bottom:
        parts.append(f"{state.scroll.unseen_bytes} B behind")
    return " · ".join(parts)


def status_announcement(state: TerminalViewState) -> str:
    """Stable screen-reader label for the terminal status.

    Unlike `status_line`, this never carries the byte offset or the
    unseen byte count: both change on every data chunk, which would
    flood the live region per chunk. Only connection status, lease
    mode, and scroll stickiness are announced.
    """
    lease = "no lease" if state.lease_mode == "none" else state.lease_mode
    parts = [state.status, lease]
    parts.append(
        "viewing latest" if state.scroll.stick_to_bottom else "scrolled back, new output arriving"
    )
    return " · ".join(parts)


@ft.component
def TerminalScreen(
    state: TerminalViewState,
    on_jump_to_bottom: Callable[[], Any],
    on_size_report: Callable[[int, int], Any],
    on_input: Callable[[int, bytes], Any],
    *,
    on_open_selection: Callable[[], Any] | None = None,
    selection_text: str = "",
    on_copy_selection: Callable[[int, int], Any] | None = None,
    on_close_selection: Callable[[], Any] | None = None,
    on_selection_change: Callable[[int, int], Any] | None = None,
    input_enabled: bool = False,
    on_terminal: Callable[[RelayDeckTerminal | None], Any] | None = None,
    on_mode_report: Callable[[bool], Any] | None = None,
    dark: bool = True,
    on_key: Callable[[str], Any] | None = None,
    keys_enabled: bool = False,
    key_row: bool = True,
    show_keys: bool = True,
    on_paste: Callable[[], Any] | None = None,
    paste_enabled: bool = False,
    on_more_keys: Callable[[], Any] | None = None,
    more_keys_open: bool = False,
    font_size: float | None = None,
    placeholder: str | None = None,
    phone: bool = False,
    hide_shortcuts: bool = False,
    paste_key: str = "terminal-paste",
    more_keys_key: str = "terminal-more-keys",
    compact: bool = False,
    touch: bool = False,
    hide_status: bool = False,
    scroll_state: ViewState | None = None,
    scroll_binding: Any = None,
    on_view_report: Callable[[RelayDeckTerminal, ViewState], Any] | None = None,
    on_scroll: Callable[[int | None], Any] | None = None,
    can_scroll: Callable[[], bool] | None = None,
    on_keyboard: Callable[[], Any] | None = None,
    keyboard_shown: bool = False,
    scroll_input_enabled: bool = False,
    cached_screen: str = "",
    connecting: bool = False,
    hidden: bool = False,
) -> Any:
    """Stable terminal surface host: viewport + status line + key row.

    `input_enabled` follows the workflow (Composer display-only vs
    Direct Input / shell) and is driven into the engine at runtime via
    `set_input_enabled`, not just at mount. `on_terminal` binds the
    stable instance to the session sink on mount and unbinds on unmount
    (explicit lifecycle cleanup). Extension size/input/mode listeners
    always deliver the latest callbacks and schedule awaitable results
    instead of dropping coroutines. `on_mode_report` receives the live
    engine `bracketedPasteMode` after every output write and reset;
    paste framing follows that engine truth.

    Terminal window: the status line sits inside the viewport
    (mode left, columns x rows right); the key row below carries Esc,
    Tab, Shift+Tab, arrows, Bksp, Enter, then Select text and Paste.
    `font_size` is read when the engine mounts: declarative controls are
    frozen and remounting the engine would force a reattach, so a new
    size applies to terminals opened afterwards.
    `key_row=False` (Watching) hides the row; the window shows the
    take-over panel instead. `placeholder` overlays the viewport while
    nothing streams (an agent is not attached until Take over). A
    bounded read-only selection overlay retains the engine beneath it
    and temporarily disables input.
    """
    term_box: Any = ft.use_ref(None)
    mount_revision, set_mount_revision = ft.use_state(0)
    callbacks_box: Any = ft.use_ref(None)
    callbacks_box.current = (on_size_report, on_input, on_mode_report, on_view_report)
    terminal_box: Any = ft.use_ref(None)
    bound_term_box: Any = ft.use_ref(None)
    terminal_box.current = on_terminal
    size, set_size = ft.use_state((0, 0))
    surface_key = SURFACE_KEY if mount_revision == 0 else f"{SURFACE_KEY}-mount-{mount_revision}"
    if term_box.current is None or term_box.current.key != surface_key:
        term = RelayDeckTerminal(
            key=surface_key,
            input_enabled=input_enabled,
            font_size=font_size if font_size is not None else 14.0,
        )

        def _emit_size(cols: int, rows: int) -> None:
            note_view_size(term, cols, rows)
            set_size((cols, rows))
            result = callbacks_box.current[0](cols, rows)
            if inspect.isawaitable(result):
                spawn(result, label="terminal-size-report")

        def _emit_input(origin: int, data: bytes) -> None:
            result = callbacks_box.current[1](origin, data)
            if inspect.isawaitable(result):
                spawn(result, label="terminal-input")

        def _emit_mode(enabled: bool) -> None:
            emit = callbacks_box.current[2]
            if emit is None:
                return
            result = emit(enabled)
            if inspect.isawaitable(result):
                spawn(result, label="terminal-mode-report")

        def _emit_view(value: ViewState) -> None:
            emit = callbacks_box.current[3]
            if emit is not None:
                result = emit(term, value)
                if inspect.isawaitable(result):
                    spawn(result, label="terminal-view-report")

        term.add_size_listener(_emit_size)
        term.add_input_listener(_emit_input)
        term.add_mode_listener(_emit_mode)
        term.add_view_state_listener(_emit_view)
        term.set_remount_handler(lambda: set_mount_revision(lambda revision: revision + 1))
        term_box.current = term
    term = term_box.current

    def _apply_input_mode() -> None:
        enabled = (
            (input_enabled or scroll_input_enabled)
            and state.lease_mode == "control"
            and not state.selection.open
        )
        diagnostics_logger().info(
            "input_mode enabled=%s direct=%s scroll=%s",
            enabled,
            input_enabled,
            scroll_input_enabled,
        )
        term.set_input_enabled(enabled)

    ft.use_effect(
        _apply_input_mode,
        dependencies=[
            input_enabled,
            scroll_input_enabled,
            state.lease_mode,
            state.selection.open,
            mount_revision,
        ],
    )

    def _bind_terminal() -> None:
        bound_term_box.current = term
        bound = terminal_box.current
        if bound is not None:
            result = bound(term)
            if inspect.isawaitable(result):
                spawn(result, label="terminal-bind")

    def _unbind_terminal() -> None:
        # Pinned Flet updates cleanup to this render's callback BEFORE
        # dependency cleanup. The ref names the actually bound old control.
        previous, bound_term_box.current = bound_term_box.current, None
        if previous is not None:
            previous.set_remount_handler(None)
        bound = terminal_box.current
        if bound is not None and (previous is None or not previous.retired):
            result = bound(None)
            if inspect.isawaitable(result):
                spawn(result, label="terminal-unbind")

    ft.use_effect(_bind_terminal, dependencies=[mount_revision], cleanup=_unbind_terminal)

    tokens = palette(dark)
    controls: list[Any] = []
    selection_overlay: Any = None
    if state.notice is not None:
        controls.append(
            ft.Card(
                key=NOTICE_KEY,
                content=ft.ListTile(
                    leading=ft.Icon(
                        ft.Icons.WARNING,
                        color=BANNER_TONES["warning"],
                    ),
                    title=ft.Text(state.notice.text, key=NOTICE_TEXT_KEY),
                ),
            )
        )
    line = status_line(state)
    cols, rows = size
    status_text = status_summary(state, attached=placeholder is None)
    viewport_layers: list[Any] = [ft.Container(key="terminal-engine-slot", content=term)]
    viewport_layers.append(
        ft.Container(
            key="terminal-cached-screen",
            visible=bool(cached_screen),
            ignore_interactions=True,
            bgcolor=tokens["terminal_bg"],
            content=ft.Column(
                scroll=ft.ScrollMode.AUTO,
                auto_scroll=True,
                controls=[
                    ft.Text(
                        "Updating…",
                        key="terminal-cache-updating",
                        size=11,
                        color=tokens["terminal_muted"],
                    ),
                    ft.Text(
                        cached_screen,
                        key="terminal-cache-text",
                        font_family="JetBrains Mono",
                        size=font_size or 13,
                        color=tokens["terminal_text"],
                    ),
                ],
            ),
        )
    )
    if placeholder is not None:
        viewport_layers.append(
            ft.Container(
                key="terminal-placeholder",
                alignment=ft.Alignment.CENTER,
                padding=24,
                content=ft.Text(
                    placeholder,
                    text_align=ft.TextAlign.CENTER,
                    size=13,
                    color=tokens["terminal_muted"],
                ),
            )
        )
    viewport_layers.append(
        ConnectingDots(tokens, connecting, hidden, key="terminal-connecting-layer")
    )
    viewport_layers.append(
        ScrollButtons(  # type: ignore[call-arg]  # key= is popped by @ft.component
            key="terminal-scroll-controls",
            # Touch tablets/phones have no wheel; show the scroll
            # controls on touch regardless of width class. Mouse desktops
            # show only "Jump to latest", and only when the view is not at the end
            # (a full-screen app owns its position, so it stays offered while live).
            visible=placeholder is None and (compact or touch or scroll_away(scroll_state)),
            jump_only=not (compact or touch),
            state=scroll_state or ViewState(alt=False, at_bottom=True, mouse=False),
            binding=scroll_binding,
            on_scroll=on_scroll,
            can_scroll=can_scroll,
            on_keyboard=on_keyboard,
            keyboard_shown=keyboard_shown,
        )
    )
    controls.append(
        ft.Container(
            key=AREA_KEY,
            expand=True,
            bgcolor=tokens["terminal_bg"],
            border=None if phone else ft.Border.all(1, tokens["line"]),
            border_radius=0 if phone else 18,
            clip_behavior=ft.ClipBehavior.ANTI_ALIAS,
            content=ft.Column(
                spacing=0,
                controls=[
                    ft.Container(
                        key="terminal-wrap",
                        expand=True,
                        padding=ft.Padding.only(
                            left=0 if phone else 12,
                            right=0 if phone else 12,
                            top=0 if phone else 12,
                        ),
                        content=ft.Stack(
                            key="terminal-viewport-stack",
                            expand=True,
                            fit=ft.StackFit.EXPAND,
                            controls=viewport_layers,
                        ),
                    ),
                    # Short heights drop the status line from the
                    # tree (exclusion frees its rows; a visible flag may
                    # keep layout space). Phones keep today's behavior.
                    *(
                        [
                            ft.Container(
                                key=TOOLBAR_KEY,
                                content=LiveRegion(  # type: ignore[call-arg]  # key= popped
                                    key="terminal-status-live",
                                    text=status_announcement(state),
                                    content=StatusLine(
                                        status_text,
                                        f"{cols} x {rows}" if cols and rows else "",
                                        tooltip=line,
                                        color=tokens["terminal_muted"],
                                        compact=phone,
                                    ),
                                ),
                            )
                        ]
                        if not phone and not hide_status
                        else []
                    ),
                ],
            ),
        )
    )
    if state.show_jump_to_latest and not compact:
        label = "Jump to latest"
        if state.scroll.unseen_bytes > 0:
            label += f" ({state.scroll.unseen_bytes} B)"
        controls.append(
            ft.Button(label, key=JUMP_KEY, height=MIN_TOUCH, on_click=click(on_jump_to_bottom))
        )
    # Select text is read-only, so it stays available whenever there is
    # output to read (watching, disconnected, exited); keys, Paste and
    # More keys write to the PTY and appear only with the full key row.
    read_only = not key_row and on_open_selection is not None and placeholder is None
    if not hide_shortcuts and (
        (key_row and (on_key is not None or on_open_selection is not None)) or read_only
    ):
        controls.append(
            ft.Container(
                key="terminal-shortcuts",
                padding=ft.Padding.only(top=4 if phone else 12),
                content=KeyRow(
                    keys_enabled=keys_enabled and key_row,
                    on_key=on_key if on_key is not None else _noop_key,
                    on_select_text=on_open_selection,
                    select_open=state.selection.open,
                    on_paste=on_paste if key_row else None,
                    paste_enabled=paste_enabled,
                    on_more_keys=on_more_keys if key_row else None,
                    more_keys_open=more_keys_open,
                    show_keys=key_row and show_keys and on_key is not None,
                    phone=phone,
                    select_key=SELECT_BUTTON_KEY,
                    paste_key=paste_key,
                    more_keys_key=more_keys_key,
                ),
            )
        )
    if state.selection.open:
        selection = state.selection
        on_copy = on_copy_selection if on_copy_selection is not None else _noop_range
        on_close = on_close_selection if on_close_selection is not None else _noop
        selection_overlay = ft.Container(
            key="terminal-selection-frame",
            bgcolor=tokens["surface"],
            border=_frame_border(tokens),
            border_radius=SURFACE_RADIUS,
            padding=12,
            content=SelectTextSheet(
                text=selection_text,
                anchor=selection.anchor,
                focus=selection.focus,
                on_copy=on_copy,
                on_close=on_close,
                error=selection.error,
                on_selection_change=on_selection_change,
                dark=dark,
            ),
        )
    # Stable stack ancestry retains the engine while the read-only sheet
    # takes the available terminal area. Its field has finite flex bounds.
    return ft.Stack(
        key="terminal-surface-stack",
        expand=True,
        fit=ft.StackFit.EXPAND,
        controls=[
            ft.Container(
                key="terminal-live-body",
                disabled=state.selection.open,
                content=ft.Column(
                    controls=controls,
                    expand=True,
                    spacing=0,
                    horizontal_alignment=ft.CrossAxisAlignment.STRETCH,
                ),
            ),
            *([selection_overlay] if selection_overlay is not None else []),
        ],
    )
