"""Terminal window presentation pieces.

One component family for agents and shells, phone and web. Controlled
presentation only: display props in, callbacks out. Sessions, leases,
writers and the native terminal engine stay with their owners; the
viewport is a child slot.

Top to bottom: header (name, mono host line, state pills, Disconnect,
More menu), viewport (live surface + status line), key row (Esc, Tab,
Shift+Tab, arrows, Bksp, Enter, then Select text and Paste), input row
(Compose | Direct switch plus composer or the dashed Direct strip).
Watching replaces the key and input rows with the take-over panel.

Deliberate deviations from the mock:
- Expanded windows retain their 48px controls. Compact/medium key chips
  use a 34px visual height within a 40px hit region.
- Expanded windows retain their full tools dock. Compact/medium windows
  use the shared key-bar model with sticky Ctrl/Alt and long-press keys.
- No pane switcher: multi-pane attach is not verified against the
  upstream protocol, so the header does not offer a dead control.
"""

from __future__ import annotations

import inspect
import logging
from collections.abc import Awaitable, Callable
from typing import Any

import flet as ft

from relaydeck.client.components.design_system import tokens as T
from relaydeck.client.components.design_system.fonts import FAMILY_INTERFACE, FAMILY_MONO
from relaydeck.client.components.design_system.primitives import _action_button, text_style
from relaydeck.client.diagnostics import failure_category
from relaydeck.client.features.terminal.events import click, spawn
from relaydeck.client.features.terminal.key_bar import KEY_BAR_GROUPS, KeyBarItem
from relaydeck.client.features.terminal.scrolling import ALT_SELECTION_HINT
from relaydeck.client.features.terminal.selection_area import selected_offsets
from relaydeck.client.features.terminal.selection_model import (
    display_width,
    normalize_range,
)

logger = logging.getLogger(__name__)
KEYBAR_FAILURE_MESSAGE = "Action unavailable. Check the terminal before continuing."

# -- window state words -------------------------------------------------------
STATE_WATCHING = "watching"
STATE_IN_CONTROL = "in-control"
STATE_CONNECTING = "connecting"
STATE_DISCONNECTED = "disconnected"
STATE_EXITED = "exited"

STATE_LABELS = {
    STATE_WATCHING: "Watching",
    STATE_IN_CONTROL: "Live · in control",
    STATE_CONNECTING: "Connecting",
    STATE_DISCONNECTED: "Disconnected",
    STATE_EXITED: "Exited",
}

TYPE_WINDOW_TITLE = {"size": 22, "weight": 800, "height": 26 / 22}
TYPE_STATE_PILL = {"size": 12, "weight": 800, "height": 28 / 12}
TYPE_KEY = {"size": 12.5, "weight": 600, "height": 1.2}
TYPE_KEY_STRONG = {"size": 12.5, "weight": 700, "height": 1.2}
TYPE_ROW_TITLE = {"size": 15, "weight": 600, "height": 20 / 15}
TYPE_ROW_HELP = {"size": 13, "weight": 400, "height": 18 / 13}
TYPE_PANEL_TITLE = {"size": 20, "weight": 800, "height": 26 / 20}
TYPE_PROMPT_TITLE = {"size": 18, "weight": 800, "height": 24 / 18}
TYPE_BODY = {"size": 14, "weight": 400, "height": 20 / 14}
TYPE_STATUS_LINE = {"size": 11.5, "weight": 400, "height": 16 / 11.5}

# "#E9E5FA" is the mock's terminal foreground and Live label.
TERMINAL_TEXT = T.TERMINAL_TEXT
STATUS_RULE = T.STATUS_RULE
WHITE = T.ON_PRIMARY

# Key row, in design order. Arrow keys render as icons.
KEY_ROW: tuple[tuple[str, str, ft.IconData | None], ...] = (
    ("escape", "Esc", None),
    ("tab", "Tab", None),
    ("shift-tab", "Shift+Tab", None),
    ("left", "Arrow left", ft.Icons.ARROW_BACK),
    ("up", "Arrow up", ft.Icons.ARROW_UPWARD),
    ("down", "Arrow down", ft.Icons.ARROW_DOWNWARD),
    ("right", "Arrow right", ft.Icons.ARROW_FORWARD),
    ("backspace", "Bksp", None),
    ("enter", "Enter", None),
)


def key_button_id(name: str) -> str:
    """Stable key-row control key (unchanged from the pre-redesign row)."""
    return f"shortcut-key-{name}"


def _disable(control: Any, disabled: bool) -> Any:
    control.disabled = disabled
    return control


def StatePill(state: str, key: str | None = None, *, compact: bool = False) -> Any:
    """Header pill for the window state: lime when in control, outlined
    otherwise. Dark text on lime (never white on lime)."""
    label = STATE_LABELS.get(state, state)
    if compact and state == STATE_IN_CONTROL:
        label = "Live"
    live = state == STATE_IN_CONTROL
    return ft.Container(
        key=key,
        padding=ft.Padding.symmetric(horizontal=8 if compact else 12),
        border_radius=ft.BorderRadius.all(14),
        bgcolor=T.DONE if live else T.RAISED,
        border=None if live else ft.Border.all(1, T.BORDER_DASHED_OFFLINE),
        alignment=ft.Alignment.CENTER,
        content=ft.Text(
            label.upper(),
            style=text_style(
                FAMILY_INTERFACE, TYPE_STATE_PILL, T.DONE_TEXT if live else TERMINAL_TEXT, 0.05
            ),
            no_wrap=True,
        ),
    )


def PillButton(
    label: str,
    on_press: Callable[[], Any],
    *,
    key: str | None = None,
    tone: str = "ghost",
    icon: ft.IconData | None = None,
    disabled: bool = False,
    tooltip: str | None = None,
    selected: bool = False,
) -> Any:
    """Fully rounded 48px button.

    tones: "ghost" (card fill, border), "outline" (violet outline, light
    violet text: Disconnect, Copy), "primary" (violet fill, white text).
    `selected` marks a pressed toggle (Select text while open).
    """
    if tone == "primary":
        fill, ink, border = T.PRIMARY_FILL, WHITE, None
    elif tone == "outline":
        fill, ink, border = None, T.PRIMARY_TEXT_LIGHT, ft.Border.all(1, T.PRIMARY_TEXT)
    else:
        fill = T.RAISED if selected else T.CARD
        ink = T.TEXT
        border = ft.Border.all(1, T.PRIMARY_TEXT if selected else T.BORDER)
    if disabled:
        ink = T.TEXT_TERMINAL_MUTED
    weight = T.TYPE_BUTTON if tone != "ghost" else T.TYPE_BUTTON_IDLE
    body: list[Any] = []
    if icon is not None:
        body.append(ft.Icon(icon, size=18, color=ink))
    body.append(ft.Text(label, style=text_style(FAMILY_INTERFACE, weight, ink), no_wrap=True))
    button = _action_button(
        key=key,
        height=T.MIN_TOUCH,
        radius=24,
        tooltip=tooltip or label,
        on_press=on_press,
        content=ft.Container(
            padding=ft.Padding.symmetric(horizontal=16),
            border_radius=ft.BorderRadius.all(24),
            bgcolor=fill,
            border=border,
            alignment=ft.Alignment.CENTER,
            content=ft.Row(tight=True, spacing=8, controls=body),
        ),
    )
    return _disable(button, disabled)


def KeyButton(
    name: str,
    label: str,
    icon: ft.IconData | None,
    on_key: Callable[[str], Any],
    enabled: bool,
) -> Any:
    """One key-row key: 12px radius raised key; Enter is the only violet key."""
    enter = name == "enter"
    ink = WHITE if enter else T.TEXT
    if not enabled:
        ink = T.TEXT_TERMINAL_MUTED
    content: Any
    if icon is not None:
        content = ft.Icon(icon, size=18, color=ink)
    else:
        content = ft.Text(
            label,
            style=text_style(FAMILY_MONO, TYPE_KEY_STRONG if enter else TYPE_KEY, ink),
            no_wrap=True,
        )
    button = _action_button(
        key=key_button_id(name),
        width=T.MIN_TOUCH if icon is not None else None,
        height=T.MIN_TOUCH,
        radius=T.RADIUS_KEY,
        tooltip=label if icon is not None else f"Send {label}",
        on_press=lambda: on_key(name),
        content=ft.Container(
            padding=ft.Padding.symmetric(horizontal=0 if icon is not None else 12),
            border_radius=ft.BorderRadius.all(T.RADIUS_KEY),
            bgcolor=T.PRIMARY_FILL if enter else T.RAISED,
            border=None if enter else ft.Border.all(1, T.BORDER),
            alignment=ft.Alignment.CENTER,
            content=content,
        ),
    )
    return _disable(button, not enabled)


def KeyRow(
    *,
    keys_enabled: bool,
    on_key: Callable[[str], Any],
    on_select_text: Callable[[], Any] | None,
    select_open: bool,
    on_paste: Callable[[], Any] | None,
    paste_enabled: bool,
    on_more_keys: Callable[[], Any] | None,
    more_keys_open: bool,
    show_keys: bool = True,
    phone: bool = False,
    select_key: str = "terminal-select-text",
    paste_key: str = "terminal-paste",
    more_keys_key: str = "terminal-more-keys",
) -> Any:
    """Key row: keys on the left, Select text / Paste at the right end.

    `show_keys` follows the "Show the key row" setting; Select text and
    Paste stay available either way. On a phone everything sits in one
    row that scrolls sideways.
    """
    keys: list[Any] = []
    if show_keys:
        keys = [KeyButton(name, label, icon, on_key, keys_enabled) for name, label, icon in KEY_ROW]
    actions: list[Any] = []
    if on_more_keys is not None and show_keys:
        actions.append(
            PillButton(
                "More keys",
                on_more_keys,
                key=more_keys_key,
                icon=ft.Icons.KEYBOARD,
                selected=more_keys_open,
                tooltip="Hide modifier keys" if more_keys_open else "Ctrl, Alt and more keys",
            )
        )
    if on_select_text is not None:
        actions.append(
            PillButton(
                "Select text",
                on_select_text,
                key=select_key,
                icon=ft.Icons.TEXT_FIELDS,
                selected=select_open,
                tooltip="Select terminal text",
            )
        )
    if on_paste is not None:
        actions.append(
            PillButton(
                "Paste",
                on_paste,
                key=paste_key,
                icon=ft.Icons.CONTENT_PASTE,
                disabled=not paste_enabled,
                tooltip="Paste into the terminal",
            )
        )
    if phone:
        return ft.Row(
            key="terminal-key-row",
            scroll=ft.ScrollMode.AUTO,
            spacing=6,
            controls=[*keys, *actions],
        )
    # The two groups wrap as units (outer Wrap is bounded by the window
    # column). Groups themselves are plain rows: in Flet 1.0.3 a Wrap
    # nested in a Row, or design-system buttons inside a Wrap, broke
    # layout (grey error box / full-width buttons).
    return ft.Row(
        key="terminal-key-row",
        wrap=True,
        spacing=10,
        run_spacing=8,
        alignment=ft.MainAxisAlignment.SPACE_BETWEEN,
        controls=[
            ft.Row(key="terminal-keys", tight=True, spacing=6, controls=keys),
            ft.Row(key="terminal-key-actions", tight=True, spacing=6, controls=actions),
        ],
    )


def ConnectionDot(live: bool, text: str, action: Callable[[], Any] | None) -> Any:
    return ft.PopupMenuButton(
        key="terminal-connection-dot",
        width=28,
        height=28,
        tooltip=text or ("Live" if live else "Connection needs attention"),
        content=ft.Container(
            alignment=ft.Alignment.CENTER,
            content=ft.Container(
                width=8, height=8, border_radius=4, bgcolor=T.DONE if live else T.PRESENCE_WARN
            ),
        ),
        items=[
            ft.PopupMenuItem(content=ft.Text(text or ("Live" if live else "Not live"))),
            *(
                [
                    ft.PopupMenuItem(
                        key="terminal-connection-retry",
                        content=ft.Text("Retry"),
                        on_click=_menu_action(action),
                    )
                ]
                if action
                else []
            ),
        ],
    )


# Bundled text fonts lack these glyphs; use Flet's Material font.
KEY_ICONS = {
    "key-left": ft.Icons.ARROW_BACK,
    "key-right": ft.Icons.ARROW_FORWARD,
    "key-up": ft.Icons.ARROW_UPWARD,
    "key-down": ft.Icons.ARROW_DOWNWARD,
    "key-backspace": ft.Icons.BACKSPACE_OUTLINED,
    "key-enter": ft.Icons.KEYBOARD_RETURN,
}


def key_chip(
    item: KeyBarItem,
    *,
    state: str,
    enabled: bool,
    on_press: Callable[[Any], Any],
    on_hold: Callable[[Any], Any] | None,
    key: str,
    tooltip: bool = False,
    size: int = 40,
) -> Any:
    """One terminal key; the key bar and the tools popover both use it."""
    icon = KEY_ICONS.get(item.id)
    label = "S-Tab" if item.id == "key-shift-tab" else item.label
    color = WHITE if state == "locked" else T.TEXT
    button = ft.TextButton(
        key=key,
        height=size,
        width=size if icon else max(size, len(label) * 8 + 16),
        disabled=not enabled,
        tooltip=(item.semantics + (" (locked)" if state == "locked" else "")) if tooltip else None,
        style=ft.ButtonStyle(
            padding=0,
            bgcolor=ft.Colors.TRANSPARENT,
            side=ft.BorderSide(0, ft.Colors.TRANSPARENT),
            shape=ft.RoundedRectangleBorder(radius=8),
            overlay_color={
                ft.ControlState.FOCUSED: T.PRIMARY_FILL,
                ft.ControlState.HOVERED: T.RAISED,
            },
        ),
        content=ft.Container(
            height=size - 6,
            alignment=ft.Alignment.CENTER,
            border_radius=8,
            bgcolor=T.PRIMARY_FILL if state == "locked" else T.RAISED,
            border=ft.Border.all(1, T.PRIMARY_TEXT_LIGHT if state == "once" else T.BORDER),
            content=ft.Icon(icon, size=16, color=color)
            if icon
            else ft.Text(label, size=13, color=color, no_wrap=True),
        ),
        on_click=on_press,
        on_long_press=on_hold,
        data={"latch": state},
    )
    return ft.Semantics(label=item.semantics, button=True, content=button)


@ft.component
def MobileKeyBar(
    *,
    on_item: Callable[[KeyBarItem, bool], Any],
    modifier_states: dict[str, str],
    enabled: bool,
    on_paste: Callable[[], Any] | None,
    on_select: Callable[[], Any] | None,
    visible: bool = True,
    medium: bool = False,
    show_keys: bool = True,
    paste_key: str = "terminal-paste",
) -> Any:
    """34px visual chips in 40px hit regions; scroll state belongs to this window."""
    offset = ft.use_ref(0.0)
    error, set_error = ft.use_state(False)
    row_ref: Any = ft.use_ref(lambda: ft.Ref[ft.Row]())

    async def perform(action: Callable[[], Any]) -> None:
        try:
            result = action()
            if inspect.isawaitable(result):
                await result
        except Exception as exc:  # noqa: BLE001 - never expose action failures in Flet's error page
            logger.error("Terminal key-bar action failed: category=%s", failure_category(exc))
            set_error(True)
        else:
            set_error(False)

    def remember(event: Any) -> None:
        offset.current = event.pixels

    async def restore() -> None:
        if visible and not medium and offset.current and row_ref.current is not None:
            await row_ref.current.current.scroll_to(offset=offset.current, duration=0)

    ft.use_effect(restore, dependencies=[visible, medium])
    groups: list[Any] = []
    for index, group in enumerate(KEY_BAR_GROUPS):
        chips: list[Any] = []
        for item in group:
            if not show_keys and item.kind != "action":
                continue
            action = on_paste if item.id == "action-paste" else on_select
            if item.kind == "action" and action is None:
                continue
            state = (
                modifier_states.get(next(iter(item.mods), ""), "off")
                if item.kind == "modifier"
                else "off"
            )
            ok = enabled or item.id == "action-select"

            async def press(
                event: Any, selected: KeyBarItem = item, callback: Any = action
            ) -> None:
                await perform(
                    lambda: callback() if selected.kind == "action" else on_item(selected, False)
                )

            async def hold(event: Any, selected: KeyBarItem = item) -> None:
                await perform(lambda: on_item(selected, True))

            button = key_chip(
                item,
                state=state,
                enabled=ok,
                on_press=press,
                on_hold=hold if item.long_press else None,
                key=paste_key
                if item.id == "action-paste"
                else "terminal-select-text"
                if item.id == "action-select"
                else item.id,
                tooltip=medium,
            )
            chips.append(button)
        if chips:
            groups.append(
                ft.Row(key=f"keybar-group-{index}", tight=True, spacing=6, controls=chips)
            )
    children: list[Any] = []
    for index, group_control in enumerate(groups):
        if index:
            children.append(
                ft.Container(
                    width=17,
                    height=18,
                    padding=ft.Padding.symmetric(horizontal=8),
                    content=ft.Container(width=1, bgcolor=T.BORDER),
                )
            )
        children.append(group_control)
    row = ft.Row(
        key="terminal-key-row",
        ref=row_ref.current,
        spacing=0,
        wrap=medium,
        run_spacing=0,
        scroll=None if medium else ft.ScrollMode.HIDDEN,
        on_scroll=remember,
        controls=children,
    )
    # A fade is a pointer-transparent sibling, never an ancestor of the engine.
    layers: list[Any] = [row]
    if not medium:
        layers.append(
            ft.Container(
                key="keybar-fade",
                right=0,
                top=0,
                bottom=0,
                width=16,
                ignore_interactions=True,
                gradient=ft.LinearGradient(
                    begin=ft.Alignment.CENTER_LEFT,
                    end=ft.Alignment.CENTER_RIGHT,
                    colors=[T.SCRIM_GRADIENT_START, T.PANEL],
                ),
            )
        )
    return ft.Column(
        key="mobile-keybar-feedback",
        visible=visible,
        spacing=4,
        tight=True,
        controls=[
            ft.Container(
                key="mobile-keybar",
                visible=visible,
                height=80 if medium else 40,
                content=ft.Stack(controls=layers),
            ),
            ft.Semantics(
                key="terminal-keybar-error-region",
                live_region=True,
                visible=error,
                content=ft.Text(KEYBAR_FAILURE_MESSAGE, key="terminal-keybar-error", size=12),
            ),
        ],
    )


def SegmentedSwitch(
    options: tuple[tuple[str, str, str], ...],
    value: str,
    on_change: Callable[[str], Any],
    *,
    key: str | None = None,
    well: str = T.CARD,
    disabled: bool = False,
) -> Any:
    """Compose | Direct style switch: 16px well, violet selected segment.

    `options` are (value, label, control key). The selected segment is
    not disabled (it stays focusable) but re-selecting it is a no-op.
    """

    def press(option: str, selected: bool) -> Callable[[], Any]:
        if selected:
            return lambda: None
        return lambda: on_change(option)

    segments: list[Any] = []
    for option, label, option_key in options:
        selected = option == value
        segment = _action_button(
            key=option_key,
            height=T.MIN_TOUCH,
            radius=12,
            tooltip=f"{label} selected" if selected else label,
            on_press=press(option, selected),
            content=ft.Container(
                padding=ft.Padding.symmetric(horizontal=18),
                border_radius=ft.BorderRadius.all(12),
                bgcolor=T.PRIMARY_FILL if selected else None,
                alignment=ft.Alignment.CENTER,
                content=ft.Text(
                    label,
                    style=text_style(
                        FAMILY_INTERFACE,
                        T.TYPE_BUTTON if selected else T.TYPE_BUTTON_IDLE,
                        WHITE if selected else T.TEXT_SECONDARY,
                    ),
                    no_wrap=True,
                ),
            ),
        )
        segment.data = {"selected": selected}
        # Selected segment is disabled, as with the pre-redesign
        # mode buttons: re-selecting the current mode is not an action.
        segments.append(_disable(segment, disabled or selected))
    return ft.Container(
        key=key,
        padding=4,
        border_radius=ft.BorderRadius.all(16),
        bgcolor=well,
        border=ft.Border.all(1, T.BORDER),
        content=ft.Row(tight=True, spacing=4, controls=segments),
    )


def DirectInputStrip(text: str, key: str = "terminal-direct-strip") -> Any:
    """Dashed "Direct input is on" strip that stands in for the composer."""
    return ft.Container(
        key=key,
        padding=ft.Padding.symmetric(horizontal=18, vertical=8),
        border_radius=ft.BorderRadius.all(27),
        bgcolor=T.CARD,
        border=ft.Border.all(1, T.BORDER_DASHED_OFFLINE),
        content=ft.Row(
            spacing=12,
            controls=[
                ft.Icon(ft.Icons.KEYBOARD_OUTLINED, size=20, color=T.WORKING),
                ft.Text(
                    text,
                    expand=True,
                    style=text_style(FAMILY_INTERFACE, TYPE_BODY, T.TEXT_SECONDARY),
                ),
            ],
        ),
    )


def _prompt_body(texts: Any, actions: list[Any], phone: bool) -> Any:
    """Text block + actions: side by side on web, stacked on a phone.
    (No flex child inside a wrapping row: Flutter's Wrap rejects it.)"""
    buttons = ft.Row(key="terminal-prompt-actions", tight=True, spacing=8, controls=actions)
    if phone:
        return ft.Column(spacing=14, tight=True, controls=[texts, buttons])
    return ft.Row(
        spacing=20,
        vertical_alignment=ft.CrossAxisAlignment.CENTER,
        controls=[ft.Container(expand=True, content=texts), buttons],
    )


@ft.component
def TakeoverPanel(
    *,
    title: str,
    message: str,
    on_take_over: Callable[[], Any],
    on_keep_watching: Callable[[], Any] | None,
    confirm: bool = True,
    busy: bool = False,
    take_over_key: str = "terminal-take-over",
    keep_watching_key: str = "terminal-keep-watching",
    keep_label: str = "Keep watching",
    take_label: str = "Take over",
    phone: bool = False,
) -> Any:
    """Take-over prompt in place of the key and input rows.

    Phone consequences wrap visibly; Details expands the full disclosure
    without changing the viewport parent. The confirm setting continues
    to control whether Keep watching is offered.
    """
    details_open, set_details_open = ft.use_state(False)
    ft.use_effect(lambda: set_details_open(False), dependencies=[message, confirm, phone])
    take = PillButton(
        "Taking over…" if busy else take_label,
        on_take_over,
        key=take_over_key,
        tone="primary",
        disabled=busy,
        tooltip=message,
    )
    if phone:
        # Never truncate the two ownership consequences on a touch screen.
        # The full disclosure is available without tooltip/long-press access.
        details = PillButton(
            "Hide details" if details_open else "Details",
            lambda: set_details_open(not details_open),
            key="terminal-takeover-details-toggle",
            selected=details_open,
            disabled=busy,
        )
        phone_actions = [details, take]
        if confirm and on_keep_watching is not None:
            phone_actions.insert(
                0,
                _action_button(
                    key=keep_watching_key,
                    width=T.MIN_TOUCH,
                    height=T.MIN_TOUCH,
                    radius=24,
                    tooltip=keep_label,
                    on_press=on_keep_watching,
                    content=ft.Icon(ft.Icons.CLOSE, color=T.TEXT),
                ),
            )
            phone_actions[0].disabled = busy
        return ft.Column(
            key="terminal-takeover-panel",
            spacing=6,
            tight=True,
            horizontal_alignment=ft.CrossAxisAlignment.STRETCH,
            controls=[
                ft.Text(
                    "May resize terminal; disconnects other clients. "
                    "While you control this pane it uses this device's size; "
                    "it returns when you leave.",
                    key="terminal-takeover-message",
                    no_wrap=False,
                    size=12,
                    color=T.TEXT_SECONDARY,
                ),
                *(
                    [
                        ft.Text(
                            message,
                            key="terminal-takeover-details",
                            no_wrap=False,
                            style=text_style(FAMILY_INTERFACE, TYPE_BODY, T.TEXT_SECONDARY),
                        )
                    ]
                    if details_open
                    else []
                ),
                ft.Row(
                    key="terminal-takeover-actions",
                    spacing=6,
                    scroll=ft.ScrollMode.AUTO,
                    controls=phone_actions,
                ),
            ],
        )
    if not confirm:
        return ft.Row(key="terminal-takeover-panel", controls=[take])
    actions: list[Any] = []
    if on_keep_watching is not None:
        actions.append(
            PillButton(
                keep_label, on_keep_watching, key=keep_watching_key, tone="outline", disabled=busy
            )
        )
    actions.append(take)
    return ft.Container(
        key="terminal-takeover-panel",
        padding=ft.Padding.symmetric(horizontal=18, vertical=16),
        border_radius=ft.BorderRadius.all(20),
        bgcolor=T.CARD,
        border=ft.Border.all(1, T.PRIMARY_FILL),
        content=_prompt_body(
            ft.Column(
                spacing=4,
                tight=True,
                controls=[
                    ft.Text(
                        title,
                        key="terminal-takeover-title",
                        style=text_style(FAMILY_INTERFACE, TYPE_PROMPT_TITLE, T.TEXT),
                    ),
                    ft.Text(
                        message,
                        key="terminal-takeover-message",
                        style=text_style(FAMILY_INTERFACE, TYPE_BODY, T.TEXT_SECONDARY),
                    ),
                ],
            ),
            actions,
            phone,
        ),
    )


def NoticePanel(
    *,
    title: str,
    message: str,
    actions: list[Any],
    key: str,
    phone: bool = False,
) -> Any:
    """Bottom-area notice (Disconnected / Exited / Close confirm): same
    frame as the take-over panel, neutral border."""
    return ft.Container(
        key=key,
        padding=ft.Padding.symmetric(horizontal=18, vertical=16),
        border_radius=ft.BorderRadius.all(20),
        bgcolor=T.CARD,
        border=ft.Border.all(1, T.BORDER),
        content=_prompt_body(
            ft.Column(
                spacing=4,
                tight=True,
                controls=[
                    ft.Text(
                        title,
                        key=f"{key}-title",
                        style=text_style(FAMILY_INTERFACE, TYPE_PROMPT_TITLE, T.TEXT),
                    ),
                    *(
                        [
                            ft.Text(
                                message,
                                key=f"{key}-message",
                                style=text_style(FAMILY_INTERFACE, TYPE_BODY, T.TEXT_SECONDARY),
                            )
                        ]
                        if message
                        else []
                    ),
                ],
            ),
            actions,
            phone,
        ),
    )


def _menu_action(action: Callable[[], Any]) -> Callable[[Any], Any]:
    async def _on_click(event: Any) -> Any:
        del event
        result = action()
        if inspect.isawaitable(result):
            return await result
        return result

    return _on_click


def MoreMenu(
    items: list[tuple[str, str, Callable[[], Any]]],
    key: str = "terminal-more",
    *,
    information: tuple[tuple[str, str], ...] = (),
    compact: bool = False,
) -> Any:
    """Round More button opening a menu of (control key, label, action)."""
    return ft.PopupMenuButton(
        key=key,
        tooltip="More actions",
        width=(28 if compact else T.MIN_TOUCH),
        height=(28 if compact else T.MIN_TOUCH),
        bgcolor=ft.Colors.TRANSPARENT if compact else T.CARD,
        menu_position=ft.PopupMenuPosition.UNDER,
        style=ft.ButtonStyle(
            shape=ft.CircleBorder(),
            bgcolor=ft.Colors.TRANSPARENT if compact else T.CARD,
            side=ft.BorderSide(0, ft.Colors.TRANSPARENT) if compact else ft.BorderSide(1, T.BORDER),
            padding=0,
        ),
        content=ft.Container(
            width=(28 if compact else T.MIN_TOUCH),
            height=(28 if compact else T.MIN_TOUCH),
            alignment=ft.Alignment.CENTER,
            content=ft.Icon(ft.Icons.MORE_HORIZ, size=16 if compact else 20, color=T.TEXT),
        ),
        items=[
            *[
                ft.PopupMenuItem(
                    disabled=True,
                    content=ft.Text(label, key=info_key, color=T.TEXT_SECONDARY),
                )
                for info_key, label in information
            ],
            *[
                ft.PopupMenuItem(
                    key=item_key,
                    content=ft.Text(
                        label, style=text_style(FAMILY_INTERFACE, T.TYPE_BUTTON_IDLE, T.TEXT)
                    ),
                    on_click=_menu_action(action),
                )
                for item_key, label, action in items
            ],
        ],
    )


def TerminalHeader(
    *,
    title: str,
    host_line: str,
    pills: list[Any],
    actions: list[Any],
    on_back: Callable[[], Any] | None = None,
    title_key: str = "terminal-window-title",
    key: str = "terminal-window-header",
    back_key: str = "terminal-back",
    phone: bool = False,
    mobile_terminal: bool = False,
    connection_text: str = "",
    connection_live: bool = True,
    on_connection_action: Callable[[], Any] | None = None,
    title_control: Any = None,
) -> Any:
    """Name + mono host line, then pills and header actions. On a phone
    the pills drop under the title so the actions keep their width; `title_control`
    replaces the phone title text (the agent switcher)."""
    compact = phone and mobile_terminal
    leading: list[Any] = []
    if on_back is not None:
        leading.append(
            _action_button(
                key=back_key,
                width=28 if compact else T.MIN_TOUCH,
                height=28 if compact else T.MIN_TOUCH,
                radius=24,
                tooltip="Back",
                on_press=on_back,
                content=ft.Container(
                    border_radius=ft.BorderRadius.all(24),
                    bgcolor=ft.Colors.TRANSPARENT if compact else T.CARD,
                    border=None if compact else ft.Border.all(1, T.BORDER),
                    alignment=ft.Alignment.CENTER,
                    content=ft.Icon(ft.Icons.ARROW_BACK, size=16 if compact else 20, color=T.TEXT),
                ),
            )
        )
    texts: list[Any] = [
        ft.Text(
            title,
            key=title_key,
            style=text_style(FAMILY_INTERFACE, TYPE_WINDOW_TITLE, T.TEXT, -0.01),
            max_lines=1,
            overflow=ft.TextOverflow.ELLIPSIS,
            tooltip=None if compact else title,
        ),
        ft.Text(
            host_line,
            key=f"{title_key}-host",
            style=text_style(FAMILY_MONO, T.TYPE_META, T.TEXT_MUTED),
            max_lines=1,
            overflow=ft.TextOverflow.ELLIPSIS,
        ),
    ]
    if phone:
        texts = [title_control] if title_control is not None else texts[:1]
        texts[0].style = text_style(FAMILY_INTERFACE, TYPE_ROW_TITLE, T.TEXT)
    return ft.Row(
        key=key,
        height=28 if compact else None,
        spacing=4 if compact else 6 if phone else 10,
        vertical_alignment=ft.CrossAxisAlignment.CENTER,
        controls=[
            *leading,
            ft.Column(spacing=0, tight=True, expand=True, controls=texts),
            *(
                [ConnectionDot(connection_live, connection_text, on_connection_action)]
                if compact
                else pills
            ),
            *actions,
        ],
    )


def StatusLine(
    left: str,
    right: str,
    tooltip: str = "",
    key: str = "terminal-status-line",
    color: str = T.TEXT_TERMINAL_MUTED,
    compact: bool = False,
) -> Any:
    """Thin line along the viewport's bottom edge: mode left, size right."""
    return ft.Container(
        key=key,
        padding=ft.Padding.symmetric(horizontal=6 if compact else 18, vertical=2 if compact else 6),
        border=ft.Border(top=ft.BorderSide(1, STATUS_RULE)),
        content=ft.Row(
            alignment=ft.MainAxisAlignment.SPACE_BETWEEN,
            controls=[
                ft.Text(
                    left,
                    key="terminal-status",
                    color=color,
                    style=text_style(FAMILY_MONO, TYPE_STATUS_LINE, color),
                    max_lines=1,
                    overflow=ft.TextOverflow.ELLIPSIS,
                    tooltip=tooltip or left,
                    expand=True,
                ),
                ft.Text(
                    right,
                    key="terminal-size",
                    style=text_style(FAMILY_MONO, TYPE_STATUS_LINE, color),
                ),
            ],
        ),
    )


def WindowFrame(controls: list[Any], *, key: str, phone: bool = False) -> Any:
    """Panel surface for the whole window (24 radius, 20px padding).

    On a phone the window is the screen itself, so there is no panel
    border or padding; the caller supplies an edge-to-edge viewport."""
    return ft.Container(
        key=key,
        expand=True,
        bgcolor=None if phone else T.PANEL,
        border=None if phone else ft.Border.all(1, T.BORDER),
        border_radius=None if phone else T.RADIUS_PANE,
        padding=0 if phone else T.PANE_PAD,
        content=ft.Column(expand=True, spacing=0 if phone else 12, controls=controls),
    )


@ft.component
def PhoneSelectionSheet(
    *,
    visible: bool,
    text: str,
    anchor: int | None,
    focus: int | None,
    error: str | None,
    binding: Any,
    on_copy: Callable[[str], Awaitable[str | None]] | None,
    on_close: Callable[[], Any],
    on_selection_change: Callable[[int, int], Any] | None,
    can_interact: Callable[[], bool] | None,
    font_size: float,
    dark: bool,
    alt: bool = False,
) -> Any:
    """Full-height native text selection; output is always literal field text."""
    # Never mirror the growing terminal log into a hidden field over Flet.
    text = text if visible else ""
    wrapped, set_wrapped = ft.use_state(True)
    result, set_result = ft.use_state("")
    current = ft.use_ref((binding, visible))
    alive = ft.use_ref(True)
    scroller: Any = ft.use_ref(lambda: ft.Ref[ft.Column]())
    current.current = (binding, visible)

    def cleanup() -> None:
        alive.current = False

    ft.use_effect(lambda: None, dependencies=[], cleanup=cleanup)

    def clear_result() -> None:
        set_result("")

    ft.use_effect(clear_result, dependencies=[binding, visible])

    def live() -> bool:
        return (
            bool(alive.current)
            and visible
            and current.current == (binding, visible)
            and (can_interact is None or can_interact())
        )

    async def newest() -> None:
        if live() and scroller.current.current is not None:
            await scroller.current.current.scroll_to(offset=-1, duration=0)

    def show_newest() -> None:
        if visible:
            spawn(newest(), label="phone-select-newest")

    ft.use_effect(show_newest, dependencies=[binding, visible])

    async def copy(mode: str) -> None:
        if not live() or on_copy is None:
            return
        try:
            written = await on_copy(mode)
        except Exception as exc:  # noqa: BLE001 - actionable clipboard failure
            if live():
                set_result(f"Copy failed: {str(exc) or type(exc).__name__}")
            return
        if live() and written is not None:
            set_result("Copied")

    async def picked(event: Any) -> None:
        if not live() or on_selection_change is None:
            return
        offsets = selected_offsets(text, event.data)
        if offsets is None:
            return
        pending = on_selection_change(*offsets)
        if inspect.isawaitable(pending):
            await pending

    def toggle_wrap() -> None:
        if live():
            set_wrapped(lambda value: not value)

    resolved = normalize_range(anchor, focus, text)
    ink = T.TEXT if dark else T.LIGHT_INK
    # A selection area over plain text: Flutter draws the highlight and handles itself.
    # A field or selectable text keeps a hidden browser input that iOS draws out of place.
    unwrapped_width = max(
        320, max((display_width(s) for s in text.splitlines()), default=0) * font_size * 0.7 + 32
    )
    field = ft.SelectionArea(
        key="phone-select-area",
        on_change=picked,
        content=ft.Text(
            text,
            key="phone-select-field",
            width=None if wrapped else unwrapped_width,
            style=ft.TextStyle(font_family=FAMILY_MONO, size=font_size, color=ink),
        ),
    )

    def action(
        label: str,
        key: str,
        callback: Callable[[], Any],
        *,
        disabled: bool = False,
        tooltip: str | None = None,
    ) -> Any:
        return ft.TextButton(
            key=key,
            height=T.MIN_TOUCH,
            expand=True,
            disabled=disabled or not visible,
            tooltip=tooltip or label,
            content=ft.Text(
                label,
                size=13,
                color=T.TEXT_MUTED if disabled and dark else ink,
                text_align=ft.TextAlign.CENTER,
            ),
            on_click=click(callback),
        )

    return ft.Container(
        key="phone-select-sheet",
        visible=visible,
        padding=12,
        bgcolor=T.PANEL if dark else T.LIGHT_SURFACE,
        content=ft.Column(
            expand=True,
            spacing=8,
            horizontal_alignment=ft.CrossAxisAlignment.STRETCH,
            controls=[
                ft.Row(
                    controls=[
                        ft.Text("Select text", size=18, color=ink, expand=True),
                        ft.TextButton(
                            "Done",
                            key="phone-select-done",
                            height=T.MIN_TOUCH,
                            on_click=click(on_close),
                        ),
                    ]
                ),
                ft.Text(
                    "Long-press text to select and use the native copy menu.", size=13, color=ink
                ),
                ft.Text(error or "", visible=bool(error), color=ink, key="phone-select-error"),
                ft.Text(
                    ALT_SELECTION_HINT,
                    visible=alt,
                    key="phone-select-alt-hint",
                    size=12,
                    color=ink,
                ),
                ft.Row(
                    key="phone-select-text-row",
                    expand=True,
                    scroll=None if wrapped else ft.ScrollMode.AUTO,
                    vertical_alignment=ft.CrossAxisAlignment.STRETCH,
                    controls=[
                        ft.Column(
                            key="phone-select-text-scroll",
                            ref=scroller.current,
                            expand=True if wrapped else None,
                            width=None if wrapped else unwrapped_width,
                            scroll=ft.ScrollMode.AUTO,
                            spacing=0,
                            horizontal_alignment=ft.CrossAxisAlignment.STRETCH,
                            controls=[field],
                        )
                    ],
                ),
                ft.Row(
                    controls=[
                        action(
                            "Wrap lines" if wrapped else "Scroll horizontally",
                            "phone-select-wrap",
                            toggle_wrap,
                        )
                    ]
                ),
                ft.Row(
                    controls=[
                        action(
                            "Copy visible screen",
                            "phone-select-copy-visible",
                            lambda: None,
                            disabled=True,
                            tooltip="Viewport snapshot support is pending; use Copy selection.",
                        ),
                        action(
                            "Copy last 100 lines",
                            "phone-select-copy-last-100",
                            lambda: copy("last-100"),
                            disabled=on_copy is None,
                        ),
                    ]
                ),
                ft.Text(
                    "Visible-screen copy is unavailable. Use the other copy actions.",
                    key="phone-select-visible-unavailable",
                    size=12,
                    color=ink,
                ),
                ft.Row(
                    controls=[
                        action(
                            "Copy all",
                            "phone-select-copy-all",
                            lambda: copy("all"),
                            disabled=on_copy is None,
                        ),
                        action(
                            "Copy selection",
                            "phone-select-copy-selection",
                            lambda: copy("selection"),
                            disabled=on_copy is None or resolved.start == resolved.end,
                        ),
                    ]
                ),
                ft.Text(
                    result,
                    key="phone-select-result",
                    visible=bool(result),
                    color=ink,
                    semantics_label=result,
                ),
            ],
        ),
    )


# -- settings controls --------------------------------------------------------


def ToggleSwitch(
    value: bool,
    on_change: Callable[[bool], Any],
    *,
    label: str,
    key: str,
) -> Any:
    """52x32 switch: lime track + dark knob right when on; dark bordered
    track + muted knob left when off."""
    knob = ft.Container(
        width=24,
        height=24,
        border_radius=ft.BorderRadius.all(12),
        bgcolor=T.DONE_TEXT if value else T.TEXT_MUTED,
    )
    track = ft.Container(
        width=52,
        height=32,
        padding=4 if value else 3,
        border_radius=ft.BorderRadius.all(16),
        bgcolor=T.DONE if value else T.RAISED,
        border=None if value else ft.Border.all(1, T.BORDER_DASHED_OFFLINE),
        alignment=ft.Alignment.CENTER_RIGHT if value else ft.Alignment.CENTER_LEFT,
        content=knob,
    )
    button = _action_button(
        key=key,
        width=56,
        height=T.MIN_TOUCH,
        radius=16,
        tooltip=f"{label}: {'on' if value else 'off'}",
        on_press=lambda: on_change(not value),
        content=ft.Container(alignment=ft.Alignment.CENTER, content=track),
    )
    button.data = {"checked": value}
    return button


def SettingRow(
    title: str,
    control: Any,
    *,
    help_text: str = "",
    first: bool = False,
    key: str | None = None,
    mono_help: bool = False,
) -> Any:
    """One settings row: title (+ helper text) left, control right."""
    texts: list[Any] = [
        ft.Text(title, style=text_style(FAMILY_INTERFACE, TYPE_ROW_TITLE, T.TEXT)),
    ]
    if help_text:
        texts.append(
            ft.Text(
                help_text,
                style=text_style(
                    FAMILY_MONO if mono_help else FAMILY_INTERFACE,
                    T.TYPE_META if mono_help else TYPE_ROW_HELP,
                    T.TEXT_MUTED,
                ),
                max_lines=2,
                overflow=ft.TextOverflow.ELLIPSIS,
            )
        )
    return ft.Container(
        key=key,
        padding=ft.Padding.symmetric(vertical=8),
        border=None if first else ft.Border(top=ft.BorderSide(1, T.RAISED)),
        content=ft.Row(
            spacing=16,
            vertical_alignment=ft.CrossAxisAlignment.CENTER,
            controls=[
                ft.Column(spacing=2, tight=True, expand=True, controls=texts),
                control,
            ],
        ),
    )


def SettingsPanel(title: str, rows: list[Any], *, key: str, phone: bool = False) -> Any:
    """Web: titled panel. Phone: small mono label over a stacked card."""
    if phone:
        return ft.Column(
            key=key,
            spacing=6,
            controls=[
                ft.Text(
                    title.upper(),
                    style=text_style(FAMILY_MONO, T.TYPE_META, T.TEXT_MUTED, 0.08),
                ),
                ft.Container(
                    padding=ft.Padding.symmetric(horizontal=14, vertical=4),
                    border_radius=ft.BorderRadius.all(T.RADIUS_TILE),
                    bgcolor=T.CARD,
                    border=ft.Border.all(1, T.BORDER),
                    content=ft.Column(spacing=0, controls=rows),
                ),
            ],
        )
    return ft.Container(
        key=key,
        padding=ft.Padding.only(left=22, right=22, top=20, bottom=12),
        border_radius=ft.BorderRadius.all(T.RADIUS_PANE),
        bgcolor=T.PANEL,
        border=ft.Border.all(1, T.BORDER),
        content=ft.Column(
            spacing=0,
            controls=[
                ft.Container(
                    padding=ft.Padding.only(bottom=8),
                    content=ft.Text(
                        title, style=text_style(FAMILY_INTERFACE, TYPE_PANEL_TITLE, T.TEXT)
                    ),
                ),
                *rows,
            ],
        ),
    )


def Stepper(
    value_label: str,
    on_minus: Callable[[], Any],
    on_plus: Callable[[], Any],
    *,
    key: str,
    can_minus: bool = True,
    can_plus: bool = True,
) -> Any:
    """Minus / value / plus stepper (terminal text size)."""

    def round_button(icon: ft.IconData, label: str, action: Callable[[], Any], ok: bool) -> Any:
        button = _action_button(
            key=f"{key}-{'minus' if icon == ft.Icons.REMOVE else 'plus'}",
            width=T.MIN_TOUCH,
            height=T.MIN_TOUCH,
            radius=24,
            tooltip=label,
            on_press=action,
            content=ft.Container(
                border_radius=ft.BorderRadius.all(24),
                bgcolor=T.CARD,
                border=ft.Border.all(1, T.BORDER),
                alignment=ft.Alignment.CENTER,
                content=ft.Icon(icon, size=18, color=T.TEXT if ok else T.TEXT_TERMINAL_MUTED),
            ),
        )
        return _disable(button, not ok)

    return ft.Row(
        key=key,
        tight=True,
        spacing=8,
        vertical_alignment=ft.CrossAxisAlignment.CENTER,
        controls=[
            round_button(ft.Icons.REMOVE, "Smaller text", on_minus, can_minus),
            ft.Container(
                width=56,
                alignment=ft.Alignment.CENTER,
                content=ft.Text(
                    value_label,
                    key=f"{key}-value",
                    style=text_style(FAMILY_MONO, T.TYPE_BUTTON, T.TEXT),
                ),
            ),
            round_button(ft.Icons.ADD, "Larger text", on_plus, can_plus),
        ],
    )


__all__ = [
    "KEY_ROW",
    "STATE_CONNECTING",
    "STATE_DISCONNECTED",
    "STATE_EXITED",
    "STATE_IN_CONTROL",
    "STATE_LABELS",
    "STATE_WATCHING",
    "DirectInputStrip",
    "ConnectionDot",
    "MobileKeyBar",
    "KeyButton",
    "KeyRow",
    "MoreMenu",
    "NoticePanel",
    "PillButton",
    "SegmentedSwitch",
    "SettingRow",
    "SettingsPanel",
    "StatePill",
    "StatusLine",
    "Stepper",
    "TakeoverPanel",
    "TerminalHeader",
    "ToggleSwitch",
    "WindowFrame",
    "key_button_id",
]
