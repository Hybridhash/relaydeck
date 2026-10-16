"""Root navigation and terminal-open action builders.

Plain functions preserve hook order and control ancestry. Dependencies are
explicit so navigation cannot implicitly acquire terminal ownership."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from typing import Any

from relaydeck.client import open_timing
from relaydeck.client.features.inventory.paging import InventoryPager
from relaydeck.client.features.inventory.routing import (
    RouteBridge,
    format_route,
)
from relaydeck.client.features.sync.controller import SyncController
from relaydeck.client.features.sync.transport import HttpxGatewayTransport
from relaydeck.client.features.terminal.geometry import DEFAULT_GEOMETRY
from relaydeck.client.hooks.support import selection_host
from relaydeck.client.retained_sessions import RetainedSessions
from relaydeck.client.sessions import SessionFactory


async def _auto_take_control(session: Any) -> None:
    """Take control on open when the session supports it (agents only)."""
    take = getattr(session, "auto_take_control", None)
    if callable(take):
        await take()


def build_open_terminal(
    *,
    _revoked_now: Any,
    close_terminal: Any,
    controller: SyncController,
    destination: Any,
    foreground: bool,
    interaction_guard: Callable[[], bool] | None,
    launch_alive: Any,
    opening_box: Any,
    prefs_ref: Any,
    retained: RetainedSessions,
    selection: Any,
    session_box: Any,
    session_factory: SessionFactory,
    set_open_error: Any,
    set_terminal_open: Any,
    terminal_host_box: Any,
    terminal_kind_box: Any,
    transport: HttpxGatewayTransport,
) -> Any:
    """Open a terminal detail: acquire the retained session and publish it."""

    def open_terminal(resource_kind: str = "", resource_key: str = "") -> None:
        if not foreground or not retained.device_foreground:
            return
        if interaction_guard is not None and not interaction_guard():
            return
        if _revoked_now() or not launch_alive.current:
            set_open_error("Access revoked. Sign in again to continue.")
            return
        kind = resource_kind or {"agents": "agent", "terminals": "shell"}.get(destination, "")
        key = resource_key or selection
        current = controller.state
        resource = (
            current.inventory.agents.get(key)
            if kind == "agent"
            else current.inventory.tabs.get(key)
        )
        if resource is None or kind not in ("agent", "shell"):
            set_open_error("That session is no longer listed.")
            return
        host = current.inventory.hosts.get(resource.host_id)
        if host is None or (
            current.granted_hosts is not None and resource.host_id not in current.granted_hosts
        ):
            return
        if (
            retained.foreground is not None
            and retained.foreground.key == key
            and retained.foreground.kind == kind
        ):
            return
        timing = open_timing.begin(kind)
        previous = retained.get(resource.host_id, kind, key)
        timing.observe(previous.session.current_term if previous is not None else None)
        geometry = retained.known_geometry() or DEFAULT_GEOMETRY
        close_terminal()
        set_open_error("")
        epoch = host.epoch

        def create() -> Any:
            if kind == "agent":
                agent = current.inventory.agents[key]
                session = session_factory.make_agent(
                    host_id=resource.host_id,
                    host_epoch=epoch,
                    agent_id=agent.terminal_id,
                    target=agent.terminal_id,
                    initial_status=agent.status.value,
                    expected_revision=agent.revision,
                )
                workflow = prefs_ref.current.agents_open_in
                if workflow != session.view().workflow:
                    session.set_workflow(workflow)
                if prefs_ref.current.auto_takeover:
                    session.expect_control()
                session.open(cols=geometry[0], rows=geometry[1])
                return session
            return session_factory.make_shell(host_id=resource.host_id, host_epoch=epoch)

        async def activate() -> None:
            entry = None
            stream_job = None
            control_token = None
            control_session = None

            def before_foreground(session: Any) -> None:
                nonlocal control_token, control_session
                begin = getattr(session, "begin_control_transition", None)
                if kind == "agent" and prefs_ref.current.auto_takeover and callable(begin):
                    control_session = session
                    control_token = begin()

            open_timing.bind(timing)
            known = retained.get(resource.host_id, kind, key) is not None
            try:
                entry = await retained.acquire(
                    resource.host_id, kind, key, epoch, create, before_foreground=before_foreground
                )
                timing.mark("acquired")
                latest = controller.state
                live_host = latest.inventory.hosts.get(resource.host_id)
                if (
                    _revoked_now()
                    or (interaction_guard is not None and not interaction_guard())
                    or not launch_alive.current
                    or live_host is None
                    or live_host.epoch != epoch
                    or (
                        latest.granted_hosts is not None
                        and resource.host_id not in latest.granted_hosts
                    )
                ):
                    await retained.discard(entry)
                    return
                session = entry.session
                expect = getattr(session, "expect_control", None)
                if kind == "agent" and prefs_ref.current.auto_takeover and callable(expect):
                    expect()
                # Publish the retained/cached screen before draft storage or
                # network work. Start the safe stream job alongside rendering.
                session_box.current = session
                terminal_kind_box.current = kind
                terminal_host_box.current = resource.host_id
                set_terminal_open(key)
                cached = retained.screens.get(entry.cache_key, epoch, session.attachment)
                session.open_timing = timing
                timing.path = (
                    "paused"
                    if getattr(session, "paused", False)
                    else "warm"
                    if session.attachment is not None
                    else "known"
                    if known
                    else "cold"
                )
                timing.cache = cached is not None
                timing.mark("published")
                if not session.paused:
                    if session.attachment is not None or cached is not None:
                        stream_job = asyncio.create_task(retained.preconnect(entry))
                    elif kind == "shell" and not session.controller and not session.disposed:
                        stream_job = asyncio.create_task(
                            session.open(current.inventory.tabs[key].id, *geometry)
                        )
                if getattr(session, "paused", False):
                    doc = await transport.snapshot(resource.host_id)
                    controller.accept_snapshot(resource.host_id, doc)
                    fresh_host = controller.state.inventory.hosts.get(resource.host_id)
                    present = (
                        key in controller.state.inventory.agents
                        if kind == "agent"
                        else key in controller.state.inventory.tabs
                    )
                    if (
                        _revoked_now()
                        or not launch_alive.current
                        or (interaction_guard is not None and not interaction_guard())
                        or fresh_host is None
                        or fresh_host.epoch != epoch
                        or not present
                        or (
                            controller.state.granted_hosts is not None
                            and resource.host_id not in controller.state.granted_hosts
                        )
                    ):
                        await retained.discard(entry)
                        return
                    # Navigation resumes a known retained attachment as a viewer.
                    # Shell resume_after_pause uses explicit control recovery.
                    await retained.resume_watch(entry)
                elif kind == "agent" and session.controller is None:
                    prepare = getattr(session, "prepare_draft", None)
                    if callable(prepare):
                        await prepare()
                if interaction_guard is not None and not interaction_guard():
                    await retained.discard(entry)
                    return
                if stream_job is not None:
                    await stream_job
                if kind == "agent" and prefs_ref.current.auto_takeover:
                    await _auto_take_control(session)
            except asyncio.CancelledError:
                if entry is not None:
                    await retained.discard(entry)
                raise
            except Exception as exc:
                timing.finish("failed")
                if entry is not None:
                    await retained.discard(entry)
                if launch_alive.current:
                    code = getattr(exc, "code", "stream_error")
                    set_open_error(f"Could not open the terminal ({code}).")
                    session_box.current = None
                    terminal_kind_box.current = ""
                    terminal_host_box.current = ""
                    set_terminal_open("")
            finally:
                if control_session is not None and control_token is not None:
                    control_session.end_control_transition(control_token)
                # Whatever happened, a takeover this open expected is no longer pending.
                expect = getattr(
                    entry.session if entry is not None else None, "expect_control", None
                )
                if callable(expect) and kind == "agent":
                    expect(False)
                if stream_job is not None and not stream_job.done():
                    stream_job.cancel()
                    await asyncio.gather(stream_job, return_exceptions=True)
                if opening_box.current is asyncio.current_task():
                    opening_box.current = None

        opening_box.current = asyncio.create_task(activate())

    return open_terminal


def build_navigation_actions(
    *,
    account: str,
    app_state: Any,
    bridge: RouteBridge,
    close_terminal: Any,
    close_view_overlays: Any,
    controller: SyncController,
    destination: Any,
    go: Callable[[str], Any],
    inventory_account: Any,
    inventory_alive: Any,
    inventory_task: Any,
    on_logout: Callable[[], Any] | None,
    open_error: Any,
    open_terminal: Any,
    operations_open: Any,
    pager: InventoryPager | None,
    release_inventory: Any,
    retained: RetainedSessions,
    selection: Any,
    set_active_host: Any,
    set_destination: Any,
    set_main_screen_revision: Any,
    set_selection: Any,
    set_sheet: Any,
    set_sheet_error: Any,
    set_sheet_status: Any,
    sheet: Any,
) -> tuple[Any, ...]:
    """Navigation closures (navigate, row selection, rail) plus new-terminal and logout."""

    def navigate(dest: str, sel: str = "", *, host_override: str = "", record: bool = True) -> None:
        set_destination(dest)
        set_selection(sel)
        if dest == "settings" and sel:
            set_active_host(sel)
        elif sel and dest in ("agents", "terminals"):
            set_active_host(host_override or selection_host(app_state, dest, sel))
        bridge.set_local(dest, sel)
        # record=False keeps the current URL (no history entry). The
        # selection and bridge state still move, so the view shows the new agent.
        if record:
            go(format_route(dest, sel))

    def select_inventory_agent(key: str, *, record: bool = True) -> None:
        # record=True (list rows): one history entry per open. record=False (dock agent
        # buttons): switch without a history entry, so one back gesture
        # returns to where the owner came from. Reload reopens the URL's agent.
        if not inventory_alive.current or inventory_account.current != account:
            return
        row = pager.rows.get(key) if pager is not None else None
        if row is not None and pager is not None:
            current = controller.state
            host = current.inventory.hosts.get(row["host_id"])
            if (
                current.revoked
                or (
                    current.granted_hosts is not None
                    and row["host_id"] not in current.granted_hosts
                )
                or host is None
                or host.epoch != row["host_epoch"]
            ):
                return
            open_terminal("agent", key)
            navigate("agents", key, host_override=row["host_id"], record=record)
            inventory_task(pager.mark_read(key))
        elif pager is None:
            open_terminal("agent", key)
            navigate("agents", key, record=record)

    def select_shell(key: str) -> None:
        # Choosing a shell row is the explicit open: shells attach
        # without takeover and ask before taking over an owned one.
        open_terminal("shell", key)
        navigate("terminals", key)

    def go_destination(dest: str) -> None:
        """Rail / bottom bar opens the destination's main screen, never a session."""
        # Reselecting these main screens also closes their locally owned
        # subviews/confirmations without discarding typed setup fields.
        if dest == "settings":
            set_main_screen_revision(lambda current: current + 1)
        if (
            dest == destination
            and not selection
            and not sheet
            and not operations_open
            and not open_error
            and not retained.error
        ):
            return
        close_view_overlays()
        navigate(dest)

    def new_terminal_from_shell() -> None:
        close_terminal()
        set_sheet("new-terminal")
        set_sheet_error("")
        set_sheet_status("")

    def handle_logout() -> None:
        release_inventory()
        close_terminal("logout")
        if on_logout is not None:
            on_logout()

    return (
        go_destination,
        handle_logout,
        navigate,
        new_terminal_from_shell,
        select_inventory_agent,
        select_shell,
    )
