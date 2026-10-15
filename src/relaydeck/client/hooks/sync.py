"""Sync hooks of `Root`: sync state, controller subscription, host data effects.

Plain functions called from `Root` at the position the inline code used to occupy, so hook
order and the control tree are unchanged. Each takes only what it reads (keyword-only) and
returns the names the rest of `Root` still uses.
"""

from __future__ import annotations

import asyncio
from typing import Any

import flet as ft

from relaydeck.client.features.sync.controller import SyncController
from relaydeck.client.features.sync.transport import (
    HttpxGatewayTransport,
    TransportError,
)

# design tokens (minimal first theme, centrally configurable).
from relaydeck.domain.models import AppState


def use_sync_state(*, host_id: str, initial: Any) -> tuple[Any, ...]:
    """The active sync host and the controller-owned app state."""
    # The route is authoritative context from the first render: a deep
    # link or restored /hosts/<id> URL must sync that host, not the
    # configured default (no transient sync of the wrong host).
    initial_host = (
        initial.selection if initial.destination == "settings" and initial.selection else host_id
    )

    active_host, set_active_host = ft.use_state(initial_host)

    app_state_box: Any = ft.use_ref(None)

    app_state, set_app_state = ft.use_state(AppState())

    app_state_box.current = app_state
    return (active_host, app_state, app_state_box, set_active_host, set_app_state)


def use_sync_subscription(
    *, active_host: Any, controller: SyncController, foreground: bool, set_app_state: Any
) -> Any:
    """Subscribe to the sync controller and supervise its stream task."""
    sync_task: Any = ft.use_ref(None)

    sync_listener: Any = ft.use_ref(None)

    sync_restart: Any = ft.use_ref(None)

    def subscribe_sync() -> None:
        stop = False
        task: asyncio.Task[Any] | None = None
        controller.subscribe(set_app_state)
        sync_listener.current = (controller, set_app_state)

        def completed(done: asyncio.Task[Any]) -> None:
            if done.cancelled():
                return
            error = done.exception()  # always retrieve; no leaked task exception
            if stop:
                return
            if error is not None:
                controller.task_failed(error)
            elif hasattr(controller, "last_run_exit"):
                controller.last_run_exit = "completed"

        def start(*, explicit_retry: bool = False) -> None:
            nonlocal task
            if stop or not foreground or not active_host or controller.state.revoked:
                return
            stalled_at = getattr(controller, "gap_stalled_at", None)
            if not explicit_retry and callable(stalled_at) and stalled_at(active_host):
                return
            if task is not None and not task.done():
                return
            task = asyncio.create_task(controller.run(active_host, lambda: stop))
            task.add_done_callback(completed)

        def resumed() -> None:
            set_app_state(controller.state)
            # Auth/fatal denials remain stopped. Only an unexpected task
            # failure is eligible for a metadata-only restart on reconnect.
            if controller.state.connection.detail == "sync_task_failed":
                start()

        # Explicit banner recovery shares the exact task/lifecycle guards.
        # A stalled gap is not automatically restarted by resume events.
        sync_restart.current = start
        subscribe_resume = getattr(controller, "subscribe_resume", None)
        if callable(subscribe_resume):
            subscribe_resume(resumed)
        set_app_state(controller.state)
        start()

        def cancel() -> None:
            nonlocal stop
            stop = True
            unsubscribe_resume = getattr(controller, "unsubscribe_resume", None)
            if callable(unsubscribe_resume):
                unsubscribe_resume(resumed)
            sync_restart.current = None
            if task is not None and not task.done():
                task.cancel()

        sync_task.current = cancel

    def unsubscribe_sync() -> None:
        # use_state creates a new setter closure on each render. Remove
        # the exact subscribed function, not the latest render's setter.
        subscription = sync_listener.current
        if subscription is not None:
            subscribed_controller, listener = subscription
            subscribed_controller.unsubscribe(listener)
            sync_listener.current = None
        current = sync_task.current
        if callable(current):
            current()
            sync_task.current = None

    ft.use_effect(
        subscribe_sync, dependencies=[controller, active_host, foreground], cleanup=unsubscribe_sync
    )
    return sync_restart


def use_host_effects(
    *,
    active_host: Any,
    app_state: Any,
    app_state_box: Any,
    controller: SyncController,
    foreground: bool,
    session_box: Any,
    terminal_kind_box: Any,
    terminal_open: Any,
    transport: HttpxGatewayTransport,
) -> Any:
    """Command-context refresh for the open agent and the host grant listing fetch."""

    def refresh_command_context() -> None:
        session = session_box.current
        if session is None or terminal_kind_box.current != "agent":
            return
        current = app_state_box.current
        agent = current.inventory.agents.get(terminal_open)
        if agent is None or agent.host_id != session.host_id:
            return
        host = current.inventory.hosts.get(agent.host_id)
        session.store.update_command_context(host.epoch if host else "", agent.revision)
        if session.store.agent_status != agent.status.value:
            session.set_agent_status(agent.status.value)

    ft.use_effect(refresh_command_context, dependencies=[app_state, terminal_open])

    hosts_task: Any = ft.use_ref(None)

    def fetch_hosts() -> None:
        if not foreground:
            return
        stop = False

        async def load() -> None:
            hosts_fn = getattr(transport, "hosts", None)
            if not callable(hosts_fn):
                return
            try:
                doc = await hosts_fn()
            except TransportError:
                return
            if stop:
                return
            entries = doc.get("hosts", [])
            # Grant ownership lives in the controller: reconciling here
            # (instead of a view-local merge) keeps revoked hosts out of
            # every later emission. The emit below re-renders via the
            # sync subscription; no local set needed.
            if isinstance(entries, list):
                controller.apply_grants(entries)

        task = asyncio.create_task(load())

        def cancel() -> None:
            nonlocal stop
            stop = True
            if not task.done():
                task.cancel()

        hosts_task.current = cancel

    def cancel_hosts() -> None:
        current = hosts_task.current
        if callable(current):
            current()

    ft.use_effect(fetch_hosts, dependencies=[active_host, foreground], cleanup=cancel_hosts)
