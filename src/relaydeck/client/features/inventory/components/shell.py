"""`AppShell`: the inventory navigation component.

Owns the hooks (via `state`), derives layout once per render, builds a
`ShellContext` and calls the plain builders in `shell_panes` / `shell_frame`.
The builders are not components, so the control tree, keys, the terminal
slot ancestry and the retained-view parking are what the single function
used to build. `AppShell` stays one component because its ~40-prop
signature, the hook calls and the derived-layout block are the contract
(about 240 lines)."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

import flet as ft

from relaydeck.client.components.host_context import HostContextHeader
from relaydeck.client.components.layout import DESKTOP, PHONE, layout_mode, sidebar_width
from relaydeck.client.features.inventory.components.chips import host_chips, space_chips
from relaydeck.client.features.inventory.components.context import ShellContext
from relaydeck.client.features.inventory.components.models import DESTINATIONS, host_context
from relaydeck.client.features.inventory.components.shell_frame import (
    build_body,
    build_frame,
    build_nav,
    build_split,
)
from relaydeck.client.features.inventory.components.shell_panes import (
    apply_collapsed_strip,
    build_agents_pane,
    build_light_settings,
    build_settings_page,
    build_shells_pane,
    build_sizing,
    build_usage_pane,
    with_console_usage,
)
from relaydeck.client.features.inventory.components.state import use_filter_state, use_sidebar_state
from relaydeck.client.features.inventory.paging import InventoryFilters
from relaydeck.client.features.inventory.selectors import (
    AgentRow,
    DiagnosticsModel,
    connection_banner,
)
from relaydeck.client.features.settings.prefs import TerminalPrefs
from relaydeck.client.theme import LIST_WIDTH, palette
from relaydeck.domain.models import AppState


@ft.component
def AppShell(
    state: AppState,
    is_wide: bool,
    destination: str,
    on_destination: Callable[[str], Any],
    on_select_agent: Callable[[str], Any],
    on_select_shell: Callable[[str], Any],
    on_select_host: Callable[[str], Any],
    on_banner_action: Callable[[], Any],
    on_open_terminal: Callable[[], Any],
    on_start_agent: Callable[[], Any],
    on_new_terminal: Callable[[], Any],
    on_back: Callable[[], Any] | None = None,
    account: str = "",
    scenarios: tuple[str, ...] = (),
    on_apply_scenario: Callable[[str], Any] | None = None,
    on_logout: Callable[[], Any] | None = None,
    diagnostics: DiagnosticsModel | None = None,
    dark: bool = True,
    viewport_width: float | None = None,
    live_detail: Any = None,
    on_open_shell: Callable[[], Any] | None = None,
    paged_agents: tuple[AgentRow, ...] | None = None,
    inventory_total: int | None = None,
    inventory_busy: bool = False,
    inventory_more: bool = False,
    inventory_error: str = "",
    on_inventory_filters: Callable[[InventoryFilters], Any] | None = None,
    on_inventory_more: Callable[[], Any] | None = None,
    on_inventory_refresh: Callable[[], Any] | None = None,
    retained_views: tuple[Any, ...] = (),
    retained_active: bool = False,
    compact_terminal: bool = False,
    typing_split: bool = False,
    management: Any = None,
    usage_fetch: Any = None,
    usage_identity: Any = None,
    usage_host_id: str = "",
    on_open_usage: Callable[[], Any] | None = None,
    foreground: bool = True,
    notifications_panel: Any = None,
    prefs: TerminalPrefs | None = None,
    on_prefs: Callable[[TerminalPrefs], Any] | None = None,
    prefs_saved: bool = True,
    live_shells: frozenset[str] = frozenset(),
) -> Any:
    (
        query,
        set_query,
        shell_query,
        set_shell_query,
        shell_host,
        filter_host,
        filter_project,
        filter_status,
        set_filter_status,
        filters_open,
        set_filters_open,
        scroll_positions,
        projects,
        project,
        shell_space,
        pick_space,
        pick_host,
        pick_shell_host,
    ) = use_filter_state(
        state=state,
        prefs=prefs,
        on_prefs=on_prefs,
        on_inventory_filters=on_inventory_filters,
    )

    def hosts(host: str, *, shells: bool = False) -> Any:
        return host_chips(state, pick_shell_host if shells else pick_host, host, shells=shells)

    def spaces(host: str, *, shells: bool = False) -> Any:
        selected = shell_space if shells else (filter_project if project else "")
        return space_chips(state, pick_space, selected, host, shells=shells)

    (remembered_width, set_width, collapsed, set_collapsed) = use_sidebar_state(
        destination=destination
    )
    width = viewport_width if viewport_width is not None else (1280 if is_wide else 360)
    mode = layout_mode(width)
    has_rail = mode != PHONE
    is_wide = mode == DESKTOP
    pane_width = sidebar_width(mode, remembered_width, width)
    console_layout = dark and destination in ("agents", "terminals", "usage", "settings")
    split_gap = 20 if console_layout and is_wide else 8
    selection = state.selection.session_key
    tokens = palette(dark)
    labels = {d: label for d, label, _ in DESTINATIONS}
    banner = connection_banner(state)
    host_label, host_status, host_reachable = host_context(state)
    context_header = HostContextHeader(
        key="host-context-header",
        host_label=host_label,
        status_text=host_status,
        reachable=host_reachable,
        dark=dark,
    )  # type: ignore[call-arg]
    list_destination = destination in ("agents", "terminals")
    detail: Any = live_detail
    pane: Any

    def resize(change: float) -> None:
        if mode == DESKTOP:
            next_width = sidebar_width(mode, (pane_width or LIST_WIDTH) + change, width)
            if next_width is not None:
                set_width(next_width)

    sizing: list[Any] = []
    if is_wide and list_destination:
        sizing = build_sizing(
            dark=dark,
            is_wide=is_wide,
            list_destination=list_destination,
            mode=mode,
            pane_width=pane_width,
            resize=resize,
            set_collapsed=set_collapsed,
        )
    ctx = ShellContext(
        account=account,
        banner=banner,
        collapsed=collapsed,
        compact_terminal=compact_terminal,
        console_layout=console_layout,
        context_header=context_header,
        dark=dark,
        destination=destination,
        diagnostics=diagnostics,
        filter_host=filter_host,
        filter_project=filter_project,
        filter_status=filter_status,
        filters_open=filters_open,
        has_rail=has_rail,
        hosts=hosts,
        inventory_busy=inventory_busy,
        inventory_error=inventory_error,
        inventory_more=inventory_more,
        inventory_total=inventory_total,
        is_wide=is_wide,
        labels=labels,
        list_destination=list_destination,
        live_detail=live_detail,
        live_shells=live_shells,
        management=management,
        usage_fetch=usage_fetch,
        usage_identity=usage_identity,
        foreground=foreground,
        usage_host_id=usage_host_id,
        on_open_usage=on_open_usage,
        mode=mode,
        notifications_panel=notifications_panel,
        on_apply_scenario=on_apply_scenario,
        on_back=on_back,
        on_banner_action=on_banner_action,
        on_destination=on_destination,
        on_inventory_more=on_inventory_more,
        on_inventory_refresh=on_inventory_refresh,
        on_logout=on_logout,
        on_new_terminal=on_new_terminal,
        on_open_shell=on_open_shell,
        on_open_terminal=on_open_terminal,
        on_prefs=on_prefs,
        on_select_agent=on_select_agent,
        on_select_host=on_select_host,
        on_select_shell=on_select_shell,
        on_start_agent=on_start_agent,
        paged_agents=paged_agents,
        pane_width=pane_width,
        pick_host=pick_host,
        pick_space=pick_space,
        prefs=prefs,
        prefs_saved=prefs_saved,
        project=project,
        projects=projects,
        query=query,
        resize=resize,
        retained_active=retained_active,
        retained_views=retained_views,
        scenarios=scenarios,
        scroll_positions=scroll_positions,
        selection=selection,
        set_collapsed=set_collapsed,
        set_filter_status=set_filter_status,
        set_filters_open=set_filters_open,
        set_query=set_query,
        set_shell_query=set_shell_query,
        shell_host=shell_host,
        shell_query=shell_query,
        shell_space=shell_space,
        sizing=sizing,
        spaces=spaces,
        split_gap=split_gap,
        state=state,
        tokens=tokens,
        typing_split=typing_split,
        width=width,
    )
    console_rows: tuple[AgentRow, ...] = ()
    if destination == "agents":
        console_rows, detail, pane = build_agents_pane(ctx, detail=detail)
    elif destination == "terminals":
        detail, pane = build_shells_pane(ctx, detail=detail)
    elif destination == "usage":
        pane = build_usage_pane(ctx)
    elif dark:
        pane = build_settings_page(ctx)
    else:
        pane = build_light_settings(ctx)

    pane = with_console_usage(ctx, pane)

    showing_detail = list_destination and (is_wide or bool(selection) or live_detail is not None)
    showing_list = not list_destination or (not collapsed if is_wide else not showing_detail)
    pane_width, showing_list, pane = apply_collapsed_strip(
        ctx,
        pane=pane,
        pane_width=pane_width,
        showing_list=showing_list,
        console_rows=console_rows,
    )
    if list_destination:
        pane.width = pane_width  # presentation-only control created in this render
    if typing_split and list_destination:
        # typing mode hides the list column without touching
        # the remembered collapse state, so leaving it restores that state.
        # Any width: landscape phones hide the list and (below) the rail too.
        showing_list = False
    split = build_split(
        ctx,
        showing_list=showing_list,
        showing_detail=showing_detail,
        pane_width=pane_width,
        pane=pane,
        detail=detail,
    )
    body = build_body(
        ctx,
        showing_list=showing_list,
        showing_detail=showing_detail,
        pane_width=pane_width,
        split=split,
    )
    nav = build_nav(ctx)
    return build_frame(ctx, body=body, nav=nav, showing_detail=showing_detail)
