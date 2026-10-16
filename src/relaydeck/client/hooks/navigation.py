"""Navigation hooks of `Root`: route state, route bridge subscription, route effects.

Plain functions called from `Root` at the position the inline code used to occupy, so hook
order and the control tree are unchanged. Each takes only what it reads (keyword-only) and
returns the names the rest of `Root` still uses.
"""

from __future__ import annotations

from typing import Any

import flet as ft

from relaydeck.client.features.inventory.paging import InventoryPager
from relaydeck.client.features.inventory.routing import (
    RouteBridge,
    RouteState,
)
from relaydeck.client.features.sync.controller import SyncController

# design tokens (minimal first theme, centrally configurable).
from relaydeck.client.hooks.support import selection_host
from relaydeck.client.retained_sessions import RetainedSessions


def use_route_state(*, bridge: RouteBridge) -> tuple[Any, ...]:
    """Route-derived state: destination, selection, viewport, keyboard and operations flags."""
    initial = bridge.state

    destination, set_destination = ft.use_state(initial.destination)

    selection, set_selection = ft.use_state(initial.selection)

    is_wide, set_is_wide = ft.use_state(initial.is_wide)

    viewport, set_viewport = ft.use_state((initial.width, initial.height))

    operations_height, set_operations_height = ft.use_state(0.0)

    keyboard_open, set_keyboard_open = ft.use_state(False)

    operations_open, set_operations_open = ft.use_state(False)
    return (
        destination,
        initial,
        is_wide,
        keyboard_open,
        operations_height,
        operations_open,
        selection,
        set_destination,
        set_is_wide,
        set_keyboard_open,
        set_operations_height,
        set_operations_open,
        set_selection,
        set_viewport,
        viewport,
    )


def use_navigation(
    *,
    bridge: RouteBridge,
    cancel_typing_blur: Any,
    initial: Any,
    retained: RetainedSessions,
    set_destination: Any,
    set_is_wide: Any,
    set_keyboard_open: Any,
    set_open_error: Any,
    set_operations_open: Any,
    set_selection: Any,
    set_sheet: Any,
    set_typing_focus: Any,
    set_viewport: Any,
) -> Any:
    """Route bridge subscription: destination, selection, viewport and keyboard baseline."""
    keyboard_baseline: Any = ft.use_ref((initial.width, initial.height))

    route_context = ft.use_ref((initial.destination, initial.selection))

    def close_view_overlays() -> None:
        # Creation fields are retained separately; hiding a view never discards them.
        set_sheet("")
        set_operations_open(False)
        set_open_error("")
        retained.error = ""
        cancel_typing_blur()
        set_typing_focus(False)

    def on_bridge(route: RouteState) -> None:
        context = (route.destination, route.selection)
        if context != route_context.current:
            close_view_overlays()
            route_context.current = context
        set_destination(route.destination)
        set_selection(route.selection)
        set_is_wide(route.is_wide)
        set_viewport((route.width, route.height))
        width, height = keyboard_baseline.current
        if abs(route.width - width) >= 150:
            width, height = route.width, route.height
        else:
            height = max(height, route.height)
        keyboard_baseline.current = (width, height)
        set_keyboard_open(height - route.height >= 150)

    bridge_unsub: Any = ft.use_ref(None)

    def subscribe_bridge() -> None:
        bridge_unsub.current = bridge.subscribe(on_bridge)

    def unsubscribe_bridge() -> None:
        current = bridge_unsub.current
        if current is not None:
            current()
            bridge_unsub.current = None

    ft.use_effect(subscribe_bridge, dependencies=[], cleanup=unsubscribe_bridge)
    return close_view_overlays


def use_route_effects(
    *,
    active_host: Any,
    app_state: Any,
    controller: SyncController,
    destination: Any,
    inventory_hosts: Any,
    inventory_tick: Any,
    pager: InventoryPager | None,
    selection: Any,
    set_active_host: Any,
) -> Any:
    """Converge the controller selection and the sync host toward the route."""

    def apply_selection() -> None:
        # Converge the controller-owned selection toward the route so later sync emissions do not
        # wipe it. Revoked stays cleared; route keys missing from inventory clear and re-select on
        # data.
        if app_state.revoked:
            return
        current = app_state.selection
        desired = (current.host_id, current.session_key, current.terminal_kind)
        if destination == "settings":
            # A Settings selection is a host: Settings › Hosts selects it.
            if selection and selection in app_state.inventory.hosts:
                desired = (selection, "", "")
            else:
                desired = ("", "", "")
        elif destination == "agents":
            agent = app_state.inventory.agents.get(selection) if selection else None
            if agent is not None:
                desired = (agent.host_id, selection, "agent")
            else:
                desired = ("", "", "")
        elif destination == "terminals":
            tab = app_state.inventory.tabs.get(selection) if selection else None
            if tab is not None:
                desired = (tab.host_id, selection, "shell")
            else:
                desired = ("", "", "")
        else:
            return
        if (current.host_id, current.session_key, current.terminal_kind) != desired:
            controller.select(*desired)

    ft.use_effect(apply_selection, dependencies=[destination, selection, app_state])

    def resolve_route_host() -> None:
        # Browser back/forward and deep links drive the sync host like clicks do; the guard keeps
        # re-emits idempotent.
        known = (
            (destination == "settings" and selection in app_state.inventory.hosts)
            or (destination == "agents" and selection in app_state.inventory.agents)
            or (destination == "terminals" and selection in app_state.inventory.tabs)
        )
        resolved = selection_host(app_state, destination, selection) if selection and known else ""
        if not resolved and destination == "agents" and pager is not None:
            row = pager.rows.get(selection)
            if row is not None and inventory_hosts.get(row["host_id"]) == row["host_epoch"]:
                resolved = row["host_id"]
        if resolved and resolved != active_host:
            set_active_host(resolved)

    ft.use_effect(
        resolve_route_host, dependencies=[destination, selection, app_state, inventory_tick]
    )
