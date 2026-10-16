"""Frame of the inventory shell: split row, body stack, navigation, ground.

Plain builder functions carved out of `AppShell` (see `shell_panes`). The
terminal slot ancestry and the retained-surface layer order live here
unchanged: retained layer first, split on top, three always-present slots."""

from __future__ import annotations

from typing import Any

import flet as ft

from relaydeck.client.components.appearance import chrome_style
from relaydeck.client.components.design_system import ConsoleBottomBar, ConsoleRail, dot_tile_src
from relaydeck.client.components.layout import DESKTOP, PHONE, rail_width
from relaydeck.client.features.inventory.components.context import ShellContext
from relaydeck.client.features.inventory.components.lists import ConnectionBanner
from relaydeck.client.features.inventory.components.models import DESTINATIONS
from relaydeck.client.features.terminal.events import event_index
from relaydeck.client.theme import FONT_SIZE, LIST_WIDTH, MIN_TOUCH


def build_split(
    ctx: ShellContext,
    *,
    showing_list: Any,
    showing_detail: Any,
    pane_width: Any,
    pane: Any,
    detail: Any,
) -> Any:
    """The list / resize handle / detail row (always three slots)."""
    console_layout = ctx.console_layout
    dark = ctx.dark
    is_wide = ctx.is_wide
    list_destination = ctx.list_destination
    live_detail = ctx.live_detail
    mode = ctx.mode
    resize = ctx.resize
    retained_active = ctx.retained_active
    split_gap = ctx.split_gap
    tokens = ctx.tokens
    split = ft.Row(
        key="inventory-split",
        expand=True,
        spacing=0,
        vertical_alignment=ft.CrossAxisAlignment.STRETCH,
        controls=[
            ft.Container(
                key="inventory-sidebar",
                visible=showing_list,
                width=pane_width if list_destination and is_wide else None,
                expand=not (list_destination and is_wide),
                bgcolor=ft.Colors.TRANSPARENT if console_layout else tokens["surface"],
                padding=0 if console_layout or ctx.destination == "usage" else 12,
                content=pane,
            ),
            ft.GestureDetector(
                key="sidebar-resize",
                width=split_gap,
                visible=showing_list and mode == DESKTOP and list_destination,
                mouse_cursor=ft.MouseCursor.RESIZE_LEFT_RIGHT,
                drag_interval=16,
                on_horizontal_drag_update=lambda e: resize(e.primary_delta or 0),
                content=ft.Container(
                    alignment=ft.Alignment.CENTER,
                    content=ft.Container(width=2, bgcolor=tokens["line"], expand=True),
                ),
            ),
            ft.Container(
                key="detail-pane",
                # An active retained surface owns this area from the layer
                # beneath; an invisible slot cannot intercept its pointer.
                visible=showing_detail and not retained_active,
                expand=True,
                padding=0 if dark or live_detail is not None else 12,
                content=detail,
            ),
        ],
    )

    return split


def build_body(
    ctx: ShellContext, *, showing_list: Any, showing_detail: Any, pane_width: Any, split: Any
) -> Any:
    """Heading, banner and the retained-surface workbench stack."""
    banner = ctx.banner
    collapsed = ctx.collapsed
    compact_terminal = ctx.compact_terminal
    console_layout = ctx.console_layout
    dark = ctx.dark
    destination = ctx.destination
    is_wide = ctx.is_wide
    labels = ctx.labels
    list_destination = ctx.list_destination
    live_detail = ctx.live_detail
    mode = ctx.mode
    on_banner_action = ctx.on_banner_action
    retained_views = ctx.retained_views
    set_collapsed = ctx.set_collapsed
    split_gap = ctx.split_gap
    tokens = ctx.tokens
    typing_split = ctx.typing_split
    body: list[Any] = []
    if (live_detail is None or (is_wide and collapsed)) and not console_layout:
        # Hosts owns its coherent heading inside the branch;
        # the outer title is suppressed there but the collapse-recovery
        # button stays available on every destination.
        title_text: list[Any] = (
            []
            if destination == "hosts"
            else [
                ft.Text(
                    labels[destination],
                    key="destination-title",
                    color=tokens["text"],
                    size=FONT_SIZE["headline"],
                    weight=ft.FontWeight.W_600,
                )
            ]
        )
        if title_text or (is_wide and collapsed):
            body.append(
                ft.Row(
                    wrap=True,
                    controls=[
                        *(
                            [
                                ft.IconButton(
                                    ft.Icons.MENU,
                                    key="sidebar-show",
                                    width=MIN_TOUCH,
                                    height=MIN_TOUCH,
                                    tooltip="Show session sidebar",
                                    style=chrome_style(dark),
                                    on_click=lambda e: set_collapsed(False),
                                )
                            ]
                            if is_wide and collapsed
                            else []
                        ),
                        *title_text,
                    ],
                )
            )
    if banner is not None and not compact_terminal:
        body.append(ConnectionBanner(key="banner", model=banner, on_action=on_banner_action))  # type: ignore[call-arg]
    retained_layer = ft.Container(
        key="retained-layer",
        left=(float(pane_width or LIST_WIDTH) + (split_gap if mode == DESKTOP else 0))
        if showing_list and list_destination and is_wide
        else 0,
        right=0,
        top=0,
        bottom=0,
        content=ft.Stack(
            key="retained-surfaces",
            expand=True,
            fit=ft.StackFit.EXPAND,
            controls=list(retained_views),
        ),
    )
    # Inactive engines stay mounted behind the opaque inventory; the active surface paints last and
    # hidden ones cannot take its pointer events. Keep this child order: swapping it remounts the
    # native terminal engine when the active surface toggles.
    layers: list[ft.Control] = [retained_layer, split]
    body.append(
        ft.Container(
            key="inventory-content",
            expand=True,
            padding=0
            if compact_terminal or typing_split or destination == "usage"
            else (16 if mode == PHONE else 28)
            if console_layout
            else 0,
            content=ft.Stack(
                key="inventory-retained-workbench",
                expand=True,
                fit=ft.StackFit.EXPAND,
                controls=layers,
            ),
        )
    )

    return body


def build_nav(ctx: ShellContext) -> Any:
    """The rail or bottom bar (key "destinations")."""
    dark = ctx.dark
    destination = ctx.destination
    has_rail = ctx.has_rail
    mode = ctx.mode
    on_destination = ctx.on_destination
    tokens = ctx.tokens
    nav_index = [d for d, _, _ in DESTINATIONS].index(destination)
    if has_rail:
        nav: Any = ft.NavigationRail(
            key="destinations",
            selected_index=nav_index,
            bgcolor=tokens["rail_bg"],
            min_width=rail_width(mode),
            width=rail_width(mode),
            label_type=ft.NavigationRailLabelType.ALL,
            indicator_color=tokens["active_row_bg"],
            indicator_shape=ft.RoundedRectangleBorder(radius=8),
            destinations=[
                ft.NavigationRailDestination(icon=getattr(ft.Icons, icon), label=label)
                for _, label, icon in DESTINATIONS
            ],
            on_change=lambda e: on_destination(DESTINATIONS[event_index(e)][0]),
        )
    else:
        nav = ft.NavigationBar(
            key="destinations",
            selected_index=nav_index,
            bgcolor=tokens["surface"],
            indicator_color=tokens["active_row_bg"],
            height=64,
            destinations=[
                ft.NavigationBarDestination(icon=getattr(ft.Icons, icon), label=label)
                for _, label, icon in DESTINATIONS
            ],
            on_change=lambda e: on_destination(DESTINATIONS[event_index(e)][0]),
        )
    if dark:
        # Presentation says Console; the existing protocol/route remains agents.
        selected_nav = "console" if destination == "agents" else destination

        def select_nav(value: str) -> Any:
            return on_destination("agents" if value == "console" else value)

        nav = (
            ConsoleRail(selected_nav, select_nav)
            if has_rail
            else ConsoleBottomBar(selected_nav, select_nav)
        )
        nav.key = "destinations"

    return nav


def build_frame(ctx: ShellContext, *, body: Any, nav: Any, showing_detail: Any) -> Any:
    """Outer frame: rail slot, body column, phone nav slot, console ground."""
    dark = ctx.dark
    has_rail = ctx.has_rail
    tokens = ctx.tokens
    typing_split = ctx.typing_split
    frame = ft.Column(
        key="inventory-frame",
        expand=True,
        spacing=0,
        controls=[
            ft.Row(
                key="inventory-workbench",
                expand=True,
                spacing=0,
                vertical_alignment=ft.CrossAxisAlignment.STRETCH,
                controls=[
                    ft.Container(
                        key="rail-slot",
                        visible=has_rail and not typing_split,
                        content=nav if has_rail else None,
                    ),
                    ft.Column(key="inventory-body", expand=True, spacing=0, controls=body),
                ],
            ),
            ft.Container(
                key="phone-nav-slot",
                visible=not has_rail and not showing_detail,
                content=nav if not has_rail else None,
            ),
        ],
    )
    return ft.Container(
        key="console-ground",
        expand=True,
        bgcolor=tokens["bg"],
        image=ft.DecorationImage(src=dot_tile_src(), repeat=ft.ImageRepeat.REPEAT)
        if dark
        else None,
        content=ft.SafeArea(content=frame, expand=True),
    )
