"""Hooks of the inventory shell: filters, search, spaces and sidebar state.

Plain hook functions (called unconditionally, in the original order, from
`AppShell`) that return their results as named tuples."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import replace
from typing import Any, NamedTuple

import flet as ft

from relaydeck.client.features.inventory.paging import InventoryFilters
from relaydeck.client.features.settings.prefs import TerminalPrefs
from relaydeck.client.theme import LIST_WIDTH
from relaydeck.domain.models import AppState, scope_key


class FilterState(NamedTuple):
    """Everything `use_filter_state` hands back to `AppShell`, by its old local name."""

    query: str
    set_query: Callable[[str], Any]
    shell_query: str
    set_shell_query: Callable[[str], Any]
    shell_host: str
    filter_host: str
    filter_project: str
    filter_status: str
    set_filter_status: Callable[[str], Any]
    filters_open: bool
    set_filters_open: Callable[[bool], Any]
    scroll_positions: Any
    projects: dict[str, Any]
    project: Any
    shell_space: str
    pick_space: Callable[[str], Any]
    pick_host: Callable[[str], Any]
    pick_shell_host: Callable[[str], Any]


class SidebarState(NamedTuple):
    """Remembered sidebar width and the per-destination collapse state."""

    remembered_width: float
    set_width: Callable[[float], Any]
    collapsed: bool
    set_collapsed: Callable[[bool], None]


def use_filter_state(
    *,
    state: AppState,
    prefs: TerminalPrefs | None,
    on_prefs: Callable[[TerminalPrefs], Any] | None,
    on_inventory_filters: Callable[[InventoryFilters], Any] | None,
) -> FilterState:
    """Filter, search and space-selection hooks (the first block of `AppShell`).

    Hook order is the original: seven `use_state`, one `use_ref`, then the
    `restore_space` and `publish_filters` effects. Call it unconditionally."""
    query, set_query = ft.use_state("")
    shell_query, set_shell_query = ft.use_state("")
    shell_host, set_shell_host = ft.use_state("")
    filter_host, set_filter_host = ft.use_state("")
    pref_space = prefs.space_key if prefs is not None else ""
    filter_project, set_filter_project = ft.use_state(pref_space)
    filter_status, set_filter_status = ft.use_state("")
    filters_open, set_filters_open = ft.use_state(False)
    scroll_positions: Any = ft.use_ref({})
    projects = {
        key: workspace
        for key, workspace in state.inventory.workspaces.items()
        if not filter_host or workspace.host_id == filter_host
    }
    project = projects.get(filter_project)
    shell_project = state.inventory.workspaces.get(filter_project)
    if shell_project is not None and shell_host and shell_project.host_id != shell_host:
        shell_project = None
    shell_space = scope_key(shell_project.host_id, shell_project.id) if shell_project else ""

    def restore_space() -> None:
        set_filter_project(pref_space)

    ft.use_effect(restore_space, dependencies=[pref_space])

    def pick_space(value: str) -> None:
        set_filter_project(value)
        if on_prefs is not None:
            on_prefs(replace(prefs or TerminalPrefs(), space_key=value))

    filters = InventoryFilters(
        filter_host,
        project.host_id if project else "",
        project.id if project else "",
        filter_status,
        query,
    )

    def publish_filters() -> None:
        if on_inventory_filters is not None:
            on_inventory_filters(filters)

    ft.use_effect(publish_filters, dependencies=[filters])

    def pick_host(value: str) -> None:
        set_filter_host(value)
        if value != filter_host:
            pick_space("")

    def pick_shell_host(value: str) -> None:
        set_shell_host(value)
        if value != shell_host:
            pick_space("")

    return FilterState(
        query=query,
        set_query=set_query,
        shell_query=shell_query,
        set_shell_query=set_shell_query,
        shell_host=shell_host,
        filter_host=filter_host,
        filter_project=filter_project,
        filter_status=filter_status,
        set_filter_status=set_filter_status,
        filters_open=filters_open,
        set_filters_open=set_filters_open,
        scroll_positions=scroll_positions,
        projects=projects,
        project=project,
        shell_space=shell_space,
        pick_space=pick_space,
        pick_host=pick_host,
        pick_shell_host=pick_shell_host,
    )


def use_sidebar_state(*, destination: str) -> SidebarState:
    """Sidebar width and collapse hooks (called right after `use_filter_state`)."""
    remembered_width, set_width = ft.use_state(float(LIST_WIDTH))
    # collapse is scoped per destination so collapsing
    # Console no longer hides the Terminals list (and back-navigation
    # restores each destination's own state). Same hook count/order.
    empty_collapse: dict[str, bool] = {}
    collapsed_by_dest, set_collapsed_by_dest = ft.use_state(empty_collapse)
    collapsed = bool(collapsed_by_dest.get(destination, False))

    def set_collapsed(value: bool) -> None:
        nxt = dict(collapsed_by_dest)
        nxt[destination] = value
        set_collapsed_by_dest(nxt)

    return SidebarState(remembered_width, set_width, collapsed, set_collapsed)
