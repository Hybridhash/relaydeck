"""Per-destination list panes of the inventory shell.

Plain builder functions (not components) carved out of `AppShell` so the
control tree, keys and ancestry are exactly what the single function built.
Each reads `ShellContext` and returns the pane it used to assign inline."""

from __future__ import annotations

from typing import Any

import flet as ft

from relaydeck.client.components.appearance import chrome_style
from relaydeck.client.components.design_system import (
    CollapsedAgentTile,
    ConsoleHeader,
    CountTile,
    RoundIconButton,
)
from relaydeck.client.components.design_system.shells import (
    CollapsedShellTile,
    FilterField,
    ShellCard,
    TerminalsHeader,
)
from relaydeck.client.components.layout import DESKTOP, PHONE
from relaydeck.client.components.session_rows import empty_hint
from relaydeck.client.features.inventory.components.chips import filter_chip_row, filter_field
from relaydeck.client.features.inventory.components.context import ShellContext
from relaydeck.client.features.inventory.components.lists import (
    AgentsList,
    HostsList,
    InventoryScroll,
    SearchField,
    ShellsList,
)
from relaydeck.client.features.inventory.components.models import (
    agent_scope_label,
    diagnostics_rows,
    terminal_rows,
)
from relaydeck.client.features.inventory.components.panes import (
    DiagnosticsPane,
    SessionDetail,
    SettingsPane,
)
from relaydeck.client.features.inventory.selectors import (
    agent_names,
    agent_rows,
    host_rows,
    shell_rows,
)
from relaydeck.client.features.settings.components import SettingsPage
from relaydeck.client.features.settings.prefs import TerminalPrefs
from relaydeck.client.features.usage.panel import UsageCard, UsagePanel
from relaydeck.client.theme import LIST_WIDTH, MIN_TOUCH
from relaydeck.domain.agent_order import CONSOLE_STATUSES
from relaydeck.domain.models import scope_key
from relaydeck.version import __version__


def build_sizing(
    *,
    dark: Any,
    is_wide: Any,
    list_destination: Any,
    mode: Any,
    pane_width: Any,
    resize: Any,
    set_collapsed: Any,
) -> list[Any]:
    """Sidebar narrow/widen/collapse controls (wide list destinations only)."""
    sizing: list[Any] = []
    if is_wide and list_destination:
        sizing = [
            ft.Row(
                key="sidebar-controls",
                wrap=True,
                spacing=4,
                controls=[
                    ft.IconButton(
                        ft.Icons.CHEVRON_LEFT,
                        key="sidebar-narrow",
                        width=MIN_TOUCH,
                        height=MIN_TOUCH,
                        tooltip="Narrow session sidebar",
                        disabled=mode != DESKTOP or pane_width == 240,
                        style=chrome_style(dark),
                        on_click=lambda e: resize(-24),
                    ),
                    ft.IconButton(
                        ft.Icons.CHEVRON_RIGHT,
                        key="sidebar-widen",
                        width=MIN_TOUCH,
                        height=MIN_TOUCH,
                        tooltip="Widen session sidebar",
                        disabled=mode != DESKTOP or pane_width == 420,
                        style=chrome_style(dark),
                        on_click=lambda e: resize(24),
                    ),
                    ft.IconButton(
                        ft.Icons.MENU_OPEN,
                        key="sidebar-collapse",
                        width=MIN_TOUCH,
                        height=MIN_TOUCH,
                        tooltip="Hide session sidebar",
                        style=chrome_style(dark),
                        on_click=lambda e: set_collapsed(True),
                    ),
                ],
            )
        ]
    return sizing


def build_agents_pane(ctx: ShellContext, *, detail: Any) -> Any:
    """Console destination: header, counts, filters and the agents list."""
    context_header = ctx.context_header
    dark = ctx.dark
    filter_host = ctx.filter_host
    filter_project = ctx.filter_project
    filter_status = ctx.filter_status
    filters_open = ctx.filters_open
    hosts = ctx.hosts
    inventory_busy = ctx.inventory_busy
    inventory_error = ctx.inventory_error
    inventory_more = ctx.inventory_more
    inventory_total = ctx.inventory_total
    is_wide = ctx.is_wide
    mode = ctx.mode
    on_back = ctx.on_back
    on_inventory_more = ctx.on_inventory_more
    on_inventory_refresh = ctx.on_inventory_refresh
    on_open_shell = ctx.on_open_shell
    on_open_terminal = ctx.on_open_terminal
    on_select_agent = ctx.on_select_agent
    on_start_agent = ctx.on_start_agent
    paged_agents = ctx.paged_agents
    pane_width = ctx.pane_width
    pick_host = ctx.pick_host
    pick_space = ctx.pick_space
    project = ctx.project
    projects = ctx.projects
    query = ctx.query
    scroll_positions = ctx.scroll_positions
    selection = ctx.selection
    set_collapsed = ctx.set_collapsed
    set_filter_status = ctx.set_filter_status
    set_filters_open = ctx.set_filters_open
    set_query = ctx.set_query
    sizing = ctx.sizing
    spaces = ctx.spaces
    state = ctx.state
    tokens = ctx.tokens
    width = ctx.width
    console_rows = (
        tuple(
            row
            for row in paged_agents
            if (not filter_host or row.host_id == filter_host)
            and (not project or (row.host_id == project.host_id and row.workspace_id == project.id))
        )
        if paged_agents is not None
        else agent_rows(
            state,
            query,
            filter_host,
            project.id if project else "",
            filter_status,
            project_host=project.host_id if project else "",
        )
    )
    agent = state.inventory.agents.get(selection)
    if detail is None:
        detail = SessionDetail(
            key="session-detail",
            title=agent_names(state).get(scope_key(agent.host_id, agent.terminal_id), "")
            if agent
            else "",
            subtitle=(
                agent_scope_label(state, agent.host_id, agent.workspace_id)
                if agent and dark
                else (f"{agent.agent} · {agent.status.value}" if agent else "")
            ),
            status=agent.status.value if agent else "unknown",
            on_open_terminal=on_open_terminal,
            missing=bool(selection) and agent is None,
            on_back=None if is_wide else on_back,
            dark=dark,
            on_open_shell=on_open_shell,
        )  # type: ignore[call-arg]
    pane = ft.Container(
        key="agents-pane",
        bgcolor=ft.Colors.TRANSPARENT if dark else tokens["surface"],
        expand=True,
        content=ft.Column(
            key="agents-pane-bounds",
            expand=True,
            spacing=14 if dark else 10,
            controls=[
                *([] if dark else sizing),
                *(
                    [
                        ConsoleHeader(
                            f"{sum(host.reachable for host in state.inventory.hosts.values())} "
                            f"of {len(state.inventory.hosts)} hosts online",
                            "Console",
                            lambda: set_filters_open(not filters_open),
                            on_start_agent,
                            (lambda: set_collapsed(True)) if is_wide else None,
                            on_refresh=on_inventory_refresh,
                            count=(
                                f"{len(console_rows)} of {inventory_total} agents"
                                if inventory_total is not None
                                else f"{len(console_rows)} agents"
                            ),
                        ),
                        ft.Row(
                            key="console-counts",
                            spacing=10,
                            scroll=ft.ScrollMode.AUTO,
                            controls=[
                                ft.Container(
                                    width=max(
                                        104,
                                        (
                                            (
                                                (pane_width or LIST_WIDTH)
                                                if is_wide
                                                else width - (32 if mode == PHONE else 56)
                                            )
                                            - 20
                                        )
                                        / 3,
                                    ),
                                    content=CountTile(
                                        status,
                                        sum(row.status.value == status for row in console_rows),
                                    ),
                                )
                                for status in CONSOLE_STATUSES[:3]
                            ],
                        ),
                        ft.Text(
                            "Loaded counts · "
                            + (filter_host or "All hosts")
                            + " · "
                            + (project.label or project.id if project else "All spaces")
                            + " · "
                            + (filter_status.title() or "All statuses"),
                            key="console-count-scope",
                            visible=filters_open,
                            size=11,
                            color=tokens["muted"],
                        ),
                    ]
                    if dark
                    else [context_header]
                ),
                filter_chip_row(
                    hosts(filter_host),
                    spaces(filter_host),
                    show_hosts=len(state.inventory.hosts) > 1,
                ),
                *(
                    [
                        ft.Container(
                            key="console-keyboard-resize",
                            visible=filters_open,
                            content=ft.Row(wrap=True, controls=list(sizing[0].controls[:2])),
                        )
                    ]
                    if dark and sizing
                    else []
                ),
                ft.Row(
                    visible=filters_open or not dark,
                    controls=[
                        filter_field(
                            "inventory-host",
                            "Host",
                            filter_host,
                            [
                                ("", "All hosts"),
                                *(
                                    (host.id, host.label or host.id)
                                    for host in state.inventory.hosts.values()
                                ),
                            ],
                            pick_host,
                        ),
                        filter_field(
                            "inventory-status",
                            "Status",
                            filter_status,
                            [
                                ("", "All statuses"),
                                *(
                                    (value, value.title())
                                    for value in (
                                        "blocked",
                                        "working",
                                        "done",
                                        "idle",
                                        "unknown",
                                    )
                                ),
                            ],
                            set_filter_status,
                        ),
                    ],
                ),
                ft.Row(
                    visible=filters_open or not dark,
                    controls=[
                        filter_field(
                            "inventory-project",
                            "Space",
                            filter_project,
                            [
                                ("", "All spaces"),
                                *(
                                    (key, workspace.label or workspace.id)
                                    for key, workspace in projects.items()
                                ),
                            ],
                            pick_space,
                        )
                    ],
                ),
                ft.Container(
                    visible=filters_open or not dark,
                    content=SearchField(
                        key="agents-search",  # type: ignore[call-arg]
                        value=query,
                        on_change=set_query,
                        label="Filter agents",
                        dark=dark,
                    ),
                ),
                ft.Container(
                    expand=True,
                    content=AgentsList(  # type: ignore[call-arg]
                        scroll_positions=scroll_positions,
                        key="agents-list",
                        rows=console_rows,
                        selected=selection,
                        on_select=on_select_agent,
                        on_start_agent=on_start_agent,
                        dark=dark,
                        phone=not is_wide,
                        total=inventory_total,
                        busy=inventory_busy,
                        more=inventory_more,
                        error=inventory_error,
                        on_more=on_inventory_more,
                        on_refresh=on_inventory_refresh,
                        show_header=not dark,
                    ),
                ),
            ],
        ),
    )

    return console_rows, detail, pane


def build_shells_pane(ctx: ShellContext, *, detail: Any) -> Any:
    """Terminals destination: header, filter, chips and the shell list."""
    context_header = ctx.context_header
    dark = ctx.dark
    hosts = ctx.hosts
    is_wide = ctx.is_wide
    live_shells = ctx.live_shells
    mode = ctx.mode
    on_back = ctx.on_back
    on_new_terminal = ctx.on_new_terminal
    on_open_terminal = ctx.on_open_terminal
    on_select_shell = ctx.on_select_shell
    scroll_positions = ctx.scroll_positions
    selection = ctx.selection
    set_collapsed = ctx.set_collapsed
    set_shell_query = ctx.set_shell_query
    shell_host = ctx.shell_host
    shell_query = ctx.shell_query
    shell_space = ctx.shell_space
    sizing = ctx.sizing
    spaces = ctx.spaces
    state = ctx.state
    tokens = ctx.tokens
    tab = state.inventory.tabs.get(selection)
    if detail is None:
        tab_host = state.inventory.hosts.get(tab.host_id) if tab else None
        detail = SessionDetail(
            key="session-detail",
            title=(tab.label or tab.id) if tab else "",
            subtitle=(
                f"{(tab_host.label if tab_host else '') or (tab.host_id if tab else '')} · shell"
            )
            if tab
            else "",
            status="unknown",
            on_open_terminal=on_open_terminal,
            missing=bool(selection) and tab is None,
            on_back=None if is_wide else on_back,
            dark=dark,
            empty_title="Shell terminals",
            empty_hint="Select a shell to open its terminal.",
            open_label="Open terminal",
        )  # type: ignore[call-arg]
    if dark:
        shell_list = terminal_rows(state, shell_query, shell_host, live_shells, shell_space)
        pane = ft.Container(
            key="shells-pane",
            expand=True,
            content=ft.Column(
                key="shells-pane-bounds",
                expand=True,
                spacing=14,
                controls=[
                    TerminalsHeader(
                        len(shell_list),
                        len({row.host for row in shell_list}),
                        on_new_terminal,
                        (lambda: set_collapsed(True)) if is_wide else None,
                    ),
                    FilterField(
                        shell_query,
                        set_shell_query,
                        hint="Filter shells",
                        key="shells-search",
                    ),
                    hosts(shell_host, shells=True),
                    spaces(shell_host, shells=True),
                    InventoryScroll(
                        control_key="shells-tiles",
                        positions=scroll_positions,
                        spacing=10,
                        items=[
                            ShellCard(
                                row.row,
                                row.row.key == selection,
                                on_select_shell,
                                compact=mode == PHONE,
                            )
                            for row in shell_list
                        ]
                        or [empty_hint("shells-empty", "No shell terminals yet.")],
                    ),
                ],
            ),
        )
    else:
        pane = ft.Container(
            key="shells-pane",
            bgcolor=tokens["surface"],
            expand=True,
            content=ft.Column(
                key="shells-pane-bounds",
                expand=True,
                controls=[
                    *sizing,
                    context_header,
                    SearchField(
                        key="shells-search",
                        value=shell_query,
                        on_change=set_shell_query,
                        label="Filter shells",
                        dark=dark,
                    ),  # type: ignore[call-arg]
                    hosts(shell_host, shells=True),
                    spaces(shell_host, shells=True),
                    ft.Container(
                        expand=True,
                        content=ShellsList(  # type: ignore[call-arg]
                            scroll_positions=scroll_positions,
                            key="shells-list",
                            rows=shell_rows(state, shell_query, shell_host, shell_space),
                            selected=selection,
                            on_select=on_select_shell,
                            on_new_terminal=on_new_terminal,
                            dark=dark,
                            phone=not is_wide,
                        ),
                    ),
                ],
            ),
        )

    return detail, pane


def build_hosts_pane(ctx: ShellContext) -> Any:
    """Settings › Hosts: the host list. Managed-host actions stay in Settings' own section."""
    return HostsList(  # type: ignore[call-arg]
        key="hosts-list",
        rows=host_rows(ctx.state),
        selected=ctx.state.selection.host_id,
        on_select=ctx.on_select_host,
        dark=ctx.dark,
        phone=not ctx.is_wide,
    )


def with_console_usage(ctx: ShellContext, pane: Any) -> Any:
    """The Console (agents) pane with the usage card above it, when a usage source is wired."""
    if ctx.destination != "agents" or ctx.usage_fetch is None or ctx.on_open_usage is None:
        return pane
    return ft.Column(
        expand=True,
        spacing=8,
        controls=[
            UsageCard(  # type: ignore[call-arg]
                key="console-usage",
                fetch=ctx.usage_fetch,
                identity=ctx.usage_identity,
                host_id=ctx.usage_host_id or ctx.state.selection.host_id,
                active=ctx.foreground,
                dark=ctx.dark,
                on_open=ctx.on_open_usage,
                wide=ctx.is_wide,
            ),
            pane,
        ],
    )


def build_usage_pane(ctx: ShellContext) -> Any:
    """Usage destination: the period, space, totals, limits, daily, spaces and CLIs."""
    return ft.Container(
        key="usage-pane",
        bgcolor=ctx.tokens["surface"],
        expand=True,
        padding=0,  # UsagePanel owns its responsive page gutter.
        content=UsagePanel(  # type: ignore[call-arg]
            key="usage-page",
            fetch=ctx.usage_fetch,
            identity=ctx.usage_identity,
            host_id=ctx.usage_host_id or ctx.state.selection.host_id,
            active=ctx.foreground,
            dark=ctx.dark,
            wide=ctx.is_wide,
        ),
    )


def build_settings_page(ctx: ShellContext) -> Any:
    """Settings destination on the dark console theme."""
    account = ctx.account
    diagnostics = ctx.diagnostics
    management = ctx.management
    mode = ctx.mode
    notifications_panel = ctx.notifications_panel
    on_apply_scenario = ctx.on_apply_scenario
    on_logout = ctx.on_logout
    on_prefs = ctx.on_prefs
    prefs = ctx.prefs
    prefs_saved = ctx.prefs_saved
    scenarios = ctx.scenarios
    pane = SettingsPage(
        key="settings-page",
        prefs=prefs if prefs is not None else TerminalPrefs(),
        on_prefs=on_prefs if on_prefs is not None else (lambda value: None),
        phone=mode == PHONE,
        wide=mode == DESKTOP,
        account=account,
        version=__version__,
        on_logout=on_logout or (lambda: None),
        scenarios=scenarios,
        on_apply_scenario=on_apply_scenario,
        management=management,
        hosts_section=build_hosts_pane(ctx),
        notifications_panel=notifications_panel,
        diagnostics=diagnostics_rows(diagnostics) if diagnostics is not None else None,
        saved=prefs_saved,
    )  # type: ignore[call-arg]

    return pane


def build_light_settings(ctx: ShellContext) -> Any:
    """Settings destination on the light theme."""
    account = ctx.account
    diagnostics = ctx.diagnostics
    management = ctx.management
    notifications_panel = ctx.notifications_panel
    on_apply_scenario = ctx.on_apply_scenario
    on_logout = ctx.on_logout
    scenarios = ctx.scenarios
    pane = ft.Column(
        expand=True,
        scroll=ft.ScrollMode.AUTO,
        controls=[
            ft.Container(content=build_hosts_pane(ctx)),
            SettingsPane(
                key="settings-pane",
                account=account,
                version=__version__,
                scenarios=scenarios,
                on_apply_scenario=on_apply_scenario or (lambda name: None),
                on_logout=on_logout or (lambda: None),
            ),  # type: ignore[call-arg]
            *([ft.Container(height=520, content=management)] if management is not None else []),
            *([notifications_panel] if notifications_panel is not None else []),
            *(
                [DiagnosticsPane(key="diagnostics-pane", model=diagnostics)]  # type: ignore[call-arg]
                if diagnostics is not None
                else []
            ),
        ],
    )

    return pane


def apply_collapsed_strip(
    ctx: ShellContext, *, pane: Any, pane_width: Any, showing_list: Any, console_rows: Any
) -> Any:
    """Swap the list pane for the 80 px strip when the sidebar is collapsed."""
    collapsed = ctx.collapsed
    dark = ctx.dark
    destination = ctx.destination
    is_wide = ctx.is_wide
    live_shells = ctx.live_shells
    on_new_terminal = ctx.on_new_terminal
    on_select_agent = ctx.on_select_agent
    on_select_shell = ctx.on_select_shell
    on_start_agent = ctx.on_start_agent
    selection = ctx.selection
    set_collapsed = ctx.set_collapsed
    shell_host = ctx.shell_host
    shell_query = ctx.shell_query
    shell_space = ctx.shell_space
    state = ctx.state
    if dark and destination == "agents" and is_wide and collapsed:
        pane_width = 80
        showing_list = True
        pane = ft.Column(
            key="console-strip",
            expand=True,
            spacing=10,
            horizontal_alignment=ft.CrossAxisAlignment.CENTER,
            controls=[
                RoundIconButton(
                    ft.Icons.KEYBOARD_DOUBLE_ARROW_RIGHT,
                    "Expand agent list",
                    lambda: set_collapsed(False),
                    key="sidebar-show",
                ),
                ft.Column(
                    expand=True,
                    scroll=ft.ScrollMode.AUTO,
                    spacing=10,
                    controls=[
                        CollapsedAgentTile(
                            row.key,
                            row.title,
                            row.status.value,
                            row.key == selection,
                            on_select_agent,
                        )
                        for row in console_rows
                    ],
                ),
                RoundIconButton(
                    ft.Icons.ADD,
                    "New agent",
                    on_start_agent,
                    tone="lime",
                    key="console-new-agent-strip",
                ),
            ],
        )
    if dark and destination == "terminals" and is_wide and collapsed:
        pane_width = 80
        showing_list = True
        pane = ft.Column(
            key="shells-strip",
            expand=True,
            spacing=10,
            horizontal_alignment=ft.CrossAxisAlignment.CENTER,
            controls=[
                RoundIconButton(
                    ft.Icons.KEYBOARD_DOUBLE_ARROW_RIGHT,
                    "Expand shell list",
                    lambda: set_collapsed(False),
                    key="sidebar-show",
                ),
                ft.Column(
                    expand=True,
                    scroll=ft.ScrollMode.AUTO,
                    spacing=10,
                    controls=[
                        CollapsedShellTile(row.row, row.row.key == selection, on_select_shell)
                        for row in terminal_rows(
                            state, shell_query, shell_host, live_shells, shell_space
                        )
                    ],
                ),
                RoundIconButton(
                    ft.Icons.ADD,
                    "New terminal",
                    on_new_terminal,
                    tone="lime",
                    key="shells-new-strip",
                ),
            ],
        )

    return pane_width, showing_list, pane
