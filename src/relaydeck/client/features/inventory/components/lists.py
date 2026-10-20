"""Presentational lists: connection banner, search field, scroll helper and the
agent / shell / host lists (components, unchanged from the single module)."""

from __future__ import annotations

import asyncio
import math
from collections.abc import Callable
from typing import Any

import flet as ft

from relaydeck.client.components.appearance import chrome_style
from relaydeck.client.components.design_system import AgentCard
from relaydeck.client.components.design_system import tokens as console_tokens
from relaydeck.client.components.design_system.hosts import HostGroupLabel, HostsHeader
from relaydeck.client.components.session_rows import agent_tile, empty_hint, host_tile, shell_tile
from relaydeck.client.features.a11y.announcer import LiveRegion
from relaydeck.client.features.inventory.agent_gestures import AgentGestures
from relaydeck.client.features.inventory.rename_flow import use_rename_flow
from relaydeck.client.features.inventory.rename_wiring import card_label, shown_title, space_label
from relaydeck.client.features.inventory.selectors import AgentRow, BannerModel, HostRow, ShellRow
from relaydeck.client.features.terminal.diagnostics import diagnostics_logger
from relaydeck.client.features.terminal.events import event_text
from relaydeck.client.theme import BANNER_TONES, FONT_SIZE, MIN_TOUCH, palette
from relaydeck.domain.models import AppState

LOADING_BAR_HEIGHT = 4
LOADING_BAR_DELAY_S = 0.3  # a load that finishes sooner never shows the bar


def use_slow_flag(busy: bool) -> bool:
    """True once `busy` has lasted LOADING_BAR_DELAY_S; false again as soon as it ends."""
    slow, set_slow = ft.use_state(False)
    timer: Any = ft.use_ref(None)

    def arm() -> None:
        if busy:
            timer.current = asyncio.get_running_loop().call_later(
                LOADING_BAR_DELAY_S, lambda: set_slow(True)
            )
        else:
            set_slow(False)

    def disarm() -> None:
        if timer.current is not None:
            timer.current.cancel()
            timer.current = None

    ft.use_effect(arm, dependencies=[busy], cleanup=disarm)
    return slow and busy


@ft.component
def ConnectionBanner(model: BannerModel, on_action: Callable[[], Any]) -> Any:
    ready, set_ready = ft.use_state(model.delay_seconds == 0)
    delay_task: Any = ft.use_ref(None)
    delay_generation: Any = ft.use_ref(0)

    def cleanup_delay() -> None:
        delay_generation.current += 1
        if delay_task.current is not None:
            delay_task.current.cancel()
            delay_task.current = None

    def delay() -> None:
        cleanup_delay()
        generation = delay_generation.current
        set_ready(model.delay_seconds == 0)

        async def reveal() -> None:
            loop = asyncio.get_running_loop()
            elapsed = 0 if model.not_live_since is None else loop.time() - model.not_live_since
            await asyncio.sleep(max(0, model.delay_seconds - elapsed))
            if generation == delay_generation.current:
                set_ready(True)

        if model.delay_seconds:
            delay_task.current = asyncio.create_task(reveal(), name="relaydeck-reconnect-banner")

    # Changing reason/attempt does not restart the same outage's timer.
    ft.use_effect(
        delay, dependencies=[model.delay_seconds, model.not_live_since], cleanup=cleanup_delay
    )
    if model.delay_seconds and not ready:
        return ft.Container(key="connection-banner-pending", height=0)
    # ft.Banner requires at least one action; action-less notices use a
    # slim dark strip in the console palette (not a stock Material card).
    if not model.action:
        return ft.Container(
            key="connection-banner",
            margin=ft.Margin.only(left=28, right=28, top=12),
            padding=ft.Padding.symmetric(horizontal=16, vertical=10),
            border_radius=14,
            bgcolor=console_tokens.CARD,
            border=ft.Border.all(1, console_tokens.BORDER),
            content=ft.Row(
                spacing=10,
                controls=[
                    ft.Icon(ft.Icons.INFO_OUTLINE, size=18, color=BANNER_TONES[model.tone]),
                    ft.Container(
                        expand=True,
                        content=LiveRegion(  # type: ignore[call-arg]
                            key="banner-live",
                            text=model.text,
                            content=ft.Text(
                                model.text,
                                key="banner-text",
                                color=console_tokens.TEXT_SECONDARY,
                                size=13,
                            ),
                        ),
                    ),
                ],
            ),
        )
    return ft.Banner(
        key="connection-banner",
        leading=ft.Icon(ft.Icons.INFO, color=BANNER_TONES[model.tone]),
        content=LiveRegion(  # type: ignore[call-arg]  # key= popped by @ft.component
            key="banner-live",
            text=model.text,
            content=ft.Text(model.text, key="banner-text"),
        ),
        actions=[
            ft.TextButton(
                model.action,
                key="banner-action",
                height=MIN_TOUCH,
                on_click=lambda e: on_action(),
            )
        ],
    )


@ft.component
def SearchField(value: str, on_change: Callable[[str], Any], label: str, dark: bool = True) -> Any:
    tokens = palette(dark)
    return ft.TextField(
        key="inventory-query",
        label=label,
        value=value,
        color=tokens["text"],
        label_style=ft.TextStyle(color=tokens["muted"]),
        hint_style=ft.TextStyle(color=tokens["muted"]),
        text_size=16,
        filled=True,
        fill_color=tokens["raised"],
        cursor_color=tokens["accent_text"],
        border={
            ft.ControlState.DEFAULT: ft.OutlineInputBorder(
                border_radius=8, side=ft.BorderSide(1, tokens["outline"])
            ),
            ft.ControlState.FOCUSED: ft.OutlineInputBorder(
                border_radius=8, side=ft.BorderSide(2, tokens["accent_text"])
            ),
        },
        content_padding=12,
        on_change=lambda e: on_change(event_text(e)),
    )


@ft.component
def InventoryScroll(
    items: list[Any],
    control_key: str,
    spacing: int,
    positions: Any = None,
) -> Any:
    """Restore list position after a destination unmounts its scroll controller."""
    column: Any = ft.use_ref(lambda: ft.Ref[ft.Column]())
    alive = ft.use_ref(True)

    def remember(event: ft.OnScrollEvent) -> None:
        if alive.current and positions is not None and math.isfinite(event.pixels):
            positions.current[control_key] = max(0.0, event.pixels)

    def restore() -> None:
        offset = positions.current.get(control_key, 0.0) if positions is not None else 0.0
        if not offset:
            return

        async def run() -> None:
            # Let the mount patch reach the carrier before requesting its scroll.
            await asyncio.sleep(0.05)
            if alive.current and column.current.current is not None:
                try:
                    # Pinned scroll_to delegates here but has no timeout argument.
                    # Bound the native request; do not cancel its registry waiter.
                    await column.current.current._invoke_method(
                        "scroll_to", {"offset": offset, "duration": 0}, timeout=1
                    )
                except (TimeoutError, RuntimeError):
                    pass  # Keep the recorded position for the next explicit visit.

        asyncio.create_task(run())

    def stop() -> None:
        alive.current = False

    ft.use_effect(restore, dependencies=[], cleanup=stop)
    return ft.Column(
        key=control_key,
        ref=column.current,
        controls=items,
        spacing=spacing,
        scroll=ft.ScrollMode.AUTO,
        on_scroll=remember,
        expand=True,
    )


@ft.component
def AgentsList(
    rows: tuple[AgentRow, ...],
    selected: str,
    on_select: Callable[[str], Any],
    on_start_agent: Callable[[], Any],
    dark: bool = True,
    phone: bool = False,
    total: int | None = None,
    busy: bool = False,
    more: bool = False,
    error: str = "",
    on_more: Callable[[], Any] | None = None,
    on_refresh: Callable[[], Any] | None = None,
    show_header: bool = True,
    scroll_positions: Any = None,
    command_transport: Any = None,
    state: AppState | None = None,
    foreground: bool = True,
    pins: frozenset[str] = frozenset(),
    on_pin: Callable[[str], Any] | None = None,
) -> Any:
    flow = use_rename_flow(rows)
    show_loading = use_slow_flag(busy)
    renamable = command_transport is not None and state is not None
    tokens = palette(dark)
    # Cards whose inputs did not change keep their control instances, so a render caused by one
    # row, a session tick or an inventory update behind an open terminal diffs only what moved.
    # The callbacks read the current flow and selector through refs, never a stale render's.
    current: Any = ft.use_ref(None)
    current.current = (flow, on_select)
    cards: Any = ft.use_ref({})

    def rename_of(row: AgentRow, title: str) -> Callable[[], None]:
        return lambda: current.current[0].open_rename(row.key, title)

    def menu_of(row: AgentRow) -> Callable[[str], None]:
        def open_menu(source: str) -> None:
            diagnostics_logger().info("agent_menu open source=%s", source)
            current.current[0].set_menu(row.key)

        return open_menu

    def card_menu_of(row: AgentRow) -> Callable[[], None]:
        return lambda: menu_of(row)("card")

    def select_of(row: AgentRow) -> Callable[[], Any]:
        return lambda: current.current[1](row.key)

    def select_card(key: str) -> Any:
        return current.current[1](key)

    tiles: list[Any] = []
    kept: dict[str, tuple[tuple[Any, ...], Any]] = {}
    for row in rows:
        title = shown_title(row, flow.overrides)
        signature = (
            row,
            title,
            row.key == selected,
            row.key == flow.highlight,
            row.key in pins,
            renamable,
            phone,
            dark,
            space_label(row, state),
        )
        cached = cards.current.get(row.key)
        if cached is not None and cached[0] == signature:
            kept[row.key] = cached
            tiles.append(cached[1])
            continue
        if dark:
            tile = AgentCard(
                agent_id=row.key,
                name=title,
                subject=row.subject,
                status=row.status.value,
                subtitle=row.subtitle,
                meta_left="",
                selected=row.key == selected,
                on_select=select_card,
                agent=row.agent,
                compact=phone,
                meta_right="Unread" if row.unread else "",
                highlighted=row.key == flow.highlight,
                on_name_double_tap=rename_of(row, title) if renamable and not phone else None,
                on_long_press=card_menu_of(row) if renamable else None,
                pinned=row.key in pins,
                space=space_label(row, state),
            )
            # Set identity on this freshly built presentation control. The live
            # terminal owner below is untouched; row callbacks keep scoped IDs.
            tile.key = f"agent-{row.key}"
        else:
            tile = agent_tile(
                row,
                row.key == selected,
                select_card,
                dark,
                phone,
                title=title,
                highlighted=row.key == flow.highlight,
                on_name_double_tap=rename_of(row, title) if renamable and not phone else None,
            )
            if row.key in pins and isinstance(tile.title, ft.Control):
                tile.title = ft.Row(
                    tight=True,
                    spacing=6,
                    controls=[
                        tile.title,
                        ft.Icon(
                            ft.Icons.PUSH_PIN,
                            key=f"agent-pinned-{row.key}",
                            size=14,
                            color=tokens["muted"],
                        ),
                    ],
                )
        if renamable:
            tile = AgentGestures(
                row.key,
                tile,
                card_label(row, title, space_label(row, state), row.key in pins),
                dark,
                on_menu=menu_of(row),
                on_rename=rename_of(row, title),
                on_select=select_of(row),
            )
        kept[row.key] = (signature, tile)
        tiles.append(tile)
    cards.current = kept
    if not tiles:
        tiles.append(empty_hint("agents-empty", "No agents match these filters."))
    return ft.Column(
        key="agents-list-bounds",
        expand=True,
        controls=[
            *(
                [
                    ft.Row(
                        wrap=True,
                        controls=[
                            ft.Text(
                                f"{len(rows)} of {total} agents"
                                if total is not None
                                else f"{len(rows)} agents",
                                key="agents-count",
                                size=FONT_SIZE["body"],
                                color=tokens["text"],
                            ),
                            ft.Button(
                                "Start agent",
                                key="start-agent-open",
                                height=MIN_TOUCH,
                                style=chrome_style(dark),
                                on_click=lambda e: on_start_agent(),
                            ),
                        ],
                    )
                ]
                if show_header
                else []
            ),
            ft.Text(error, key="inventory-page-error", color=tokens["muted"], visible=bool(error)),
            # The bar overlays the list's top edge and takes no space, so cards never move; it
            # appears only when a load is slow enough to notice.
            ft.Stack(
                key="agents-list-area",
                expand=True,
                controls=[
                    InventoryScroll(
                        items=tiles,
                        control_key="agents-tiles",
                        spacing=10 if dark else 4,
                        positions=scroll_positions,
                    ),
                    ft.Container(
                        top=0,
                        left=0,
                        right=0,
                        content=ft.ProgressBar(
                            key="inventory-page-loading",
                            visible=show_loading,
                            bar_height=LOADING_BAR_HEIGHT,
                        ),
                    ),
                ],
            ),
            ft.Row(
                wrap=True,
                controls=[
                    *(
                        [
                            ft.Text(
                                f"{len(rows)} of {total} agents"
                                if total is not None
                                else f"{len(rows)} agents",
                                key="agents-count",
                                size=12,
                                color=tokens["muted"],
                            )
                        ]
                        if not show_header and not dark  # header shows it
                        else []
                    ),
                    ft.TextButton(
                        "Refresh",
                        key="inventory-refresh",
                        height=MIN_TOUCH,
                        disabled=busy,
                        # the dark Console refreshes from its header icon.
                        visible=on_refresh is not None and not dark,
                        on_click=lambda e: on_refresh() if on_refresh is not None else None,
                    ),
                    ft.TextButton(
                        "Load more",
                        key="inventory-more",
                        height=MIN_TOUCH,
                        disabled=busy,
                        visible=more,
                        on_click=lambda e: on_more() if on_more is not None else None,
                    ),
                ],
            ),
            *flow.overlays(
                rows=rows,
                state=state if renamable else None,
                transport=command_transport,
                on_select=on_select,
                on_refresh=on_refresh,
                dark=dark,
                phone=phone,
                foreground=foreground,
                pins=pins,
                on_pin=on_pin if renamable else None,
            ),
        ],
    )


@ft.component
def ShellsList(
    rows: tuple[ShellRow, ...],
    selected: str,
    on_select: Callable[[str], Any],
    on_new_terminal: Callable[[], Any],
    dark: bool = True,
    phone: bool = False,
    scroll_positions: Any = None,
) -> Any:
    tokens = palette(dark)
    tiles: list[Any] = [
        shell_tile(row, row.key == selected, on_select, dark, phone) for row in rows
    ] or [empty_hint("shells-empty", "No shell terminals yet.")]
    return ft.Column(
        key="shells-list-bounds",
        expand=True,
        controls=[
            ft.Row(
                wrap=True,
                controls=[
                    ft.Text(
                        f"{len(rows)} shells",
                        key="shells-count",
                        size=FONT_SIZE["body"],
                        color=tokens["text"],
                    ),
                    ft.Button(
                        "New terminal",
                        key="new-terminal-open",
                        height=MIN_TOUCH,
                        style=chrome_style(dark),
                        on_click=lambda e: on_new_terminal(),
                    ),
                ],
            ),
            InventoryScroll(
                items=tiles,
                control_key="shells-tiles",
                spacing=4,
                positions=scroll_positions,
            ),
        ],
    )


@ft.component
def HostsList(
    rows: tuple[HostRow, ...],
    selected: str,
    on_select: Callable[[str], Any],
    dark: bool = True,
    phone: bool = False,
) -> Any:
    tiles: list[Any] = [host_tile(row, row.key == selected, on_select, dark, phone) for row in rows]
    if not dark:
        return ft.Column(
            key="hosts-list-bounds",
            expand=True,
            controls=[
                ft.Column(
                    key="hosts-tiles",
                    controls=tiles or [ft.Text("No hosts", key="hosts-empty")],
                    spacing=4,
                    scroll=ft.ScrollMode.AUTO,
                    expand=True,
                )
            ],
        )
    online = [row for row in rows if row.reachable]
    offline = [row for row in rows if not row.reachable]

    def group(label: str, key: str, members: list[HostRow]) -> Any:
        return ft.Column(
            spacing=8,
            controls=[
                HostGroupLabel(f"{label} · {len(members)}", key=key),
                ft.Column(
                    spacing=14,
                    controls=[
                        host_tile(row, row.key == selected, on_select, dark, phone)
                        for row in members
                    ],
                ),
            ],
        )

    groups: list[Any] = []
    if online:
        groups.append(group("Connected", "hosts-group-connected", online))
    if offline:
        groups.append(group("Offline", "hosts-group-offline", offline))
    if not rows:
        groups.append(ft.Text("No hosts", key="hosts-empty"))
    return ft.Column(
        key="hosts-list-bounds",
        expand=True,
        spacing=18,
        controls=[
            HostsHeader("RelayDeck", "Hosts"),
            ft.Column(
                key="hosts-tiles",
                spacing=18,
                controls=groups,
                scroll=ft.ScrollMode.AUTO,
                expand=True,
            ),
        ],
    )
