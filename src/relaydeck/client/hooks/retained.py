"""Retained-session hooks of `Root`: open-terminal state/registry and terminal lifecycle effects.

Plain functions called from `Root` at the position the inline code used to occupy, so hook
order and the control tree are unchanged. Each takes only what it reads (keyword-only) and
returns the names the rest of `Root` still uses.
"""

from __future__ import annotations

import asyncio
import time
from typing import Any

import flet as ft
from relaydeck_terminal.diagnostics import trace_lifecycle

from relaydeck.client.features.sync.controller import SyncController

# design tokens (minimal first theme, centrally configurable).
from relaydeck.client.retained_sessions import RetainedSessions


def use_retained_sessions(
    *,
    app_state_box: Any,
    controller: SyncController,
    creation_drafts: Any,
    foreground: bool,
    management_fields: Any,
    phone: bool,
    retained_sessions: RetainedSessions | None,
    shell_attempts: Any,
    shell_origin: Any,
) -> tuple[Any, ...]:
    """Terminal-open state, the retained-session registry and the close/revocation helpers."""
    # Open terminal detail: the opened selection key is
    # state (it switches the mounted surface); the live session, its
    # host/kind, and supervised tasks live in refs (mutable by design).
    terminal_open, set_terminal_open = ft.use_state("")

    retained_tick, set_retained_tick = ft.use_state(0)

    retained_box: Any = ft.use_ref(None)

    if retained_box.current is None:
        retained_box.current = retained_sessions or RetainedSessions()

    retained: RetainedSessions = retained_box.current

    retained.changed = lambda: set_retained_tick(time.monotonic_ns())

    ft.use_effect(lambda: retained.set_device_foreground(foreground), dependencies=[foreground])

    # Rotation or a window resize across the phone breakpoint changes how many stay connected.
    ft.use_effect(lambda: retained.set_form_factor(phone), dependencies=[phone])

    expiry_task: Any = ft.use_ref(None)

    terminal_kind_box: Any = ft.use_ref("")

    terminal_host_box: Any = ft.use_ref("")

    # Same-identity agent drafts across ordinary Back/reopen (Root
    # rebuilds the session; the draft must survive). Keyed by
    # (host_id, agent_id); revocation/logout purge, ordinary
    # navigation never clears.
    draft_stash_box: Any = ft.use_ref({})

    session_box: Any = ft.use_ref(None)

    opening_box: Any = ft.use_ref(None)

    close_reason_box: Any = ft.use_ref("leave")

    open_error, set_open_error = ft.use_state("")

    def _cancel_task(box: Any) -> None:
        task = box.current
        box.current = None
        if task is not None and not task.done():
            task.cancel()

    def _revoked_now() -> bool:
        """Current revocation truth at invocation time: the render-closed
        `app_state` goes stale the moment a callback outlives its render,
        so authorization guards read the live ref plus the authoritative
        controller state (either may observe the revocation first; fail
        closed on either)."""
        if app_state_box.current.revoked:
            return True
        current = getattr(controller, "state", None)
        return bool(current is not None and current.revoked)

    def _purge_revoked_stash() -> None:
        """Drop stashed drafts the identity may no longer hold:
        everything on global revocation, only ungranted hosts on grant
        loss. Ordinary navigation never calls this."""
        stash = draft_stash_box.current
        current = app_state_box.current
        if current.revoked:
            creation_drafts.current.clear()
            management_fields.current = ("", "", "")
            stash.clear()
            shell_attempts.current.clear()
            shell_origin.current = None
            return
        granted = current.granted_hosts
        if granted is not None:
            for kind, draft in tuple(creation_drafts.current.items()):
                if draft.host_id not in granted:
                    del creation_drafts.current[kind]
            for entry in [key for key in stash if key[0] not in granted]:
                del stash[entry]
            attempts = shell_attempts.current
            for entry in [key for key in attempts if key[0] not in granted]:
                del attempts[entry]
            if shell_origin.current is not None and shell_origin.current[1] not in granted:
                shell_origin.current = None

    def close_terminal(reason: str = "leave") -> asyncio.Task[Any] | None:
        """Ordinary navigation hides a retained view; identity teardown disposes it."""
        close_reason_box.current = reason
        host = terminal_host_box.current
        retained.release()
        session_box.current = None
        terminal_kind_box.current = ""
        terminal_host_box.current = ""
        opening = opening_box.current
        opening_box.current = None
        if opening is not None and not opening.done():
            opening.cancel()
        if terminal_open:
            set_terminal_open("")
        if reason == "logout":
            creation_drafts.current.clear()
            management_fields.current = ("", "", "")
            draft_stash_box.current.clear()
            shell_attempts.current.clear()
            shell_origin.current = None
            retained.invalidate()
            return asyncio.create_task(retained.shutdown())
        if reason == "revoke":
            _purge_revoked_stash()
            scope = None if _revoked_now() else host
            retained.invalidate(scope)
            return asyncio.create_task(retained.clear(scope))
        return opening if isinstance(opening, asyncio.Task) else None

    return (
        _purge_revoked_stash,
        _revoked_now,
        close_terminal,
        expiry_task,
        open_error,
        opening_box,
        retained,
        retained_tick,
        session_box,
        set_open_error,
        set_retained_tick,
        set_terminal_open,
        terminal_host_box,
        terminal_kind_box,
        terminal_open,
    )


def use_terminal_effects(
    *,
    _purge_revoked_stash: Any,
    app_state: Any,
    close_terminal: Any,
    controller: SyncController,
    destination: Any,
    expiry_task: Any,
    retained: RetainedSessions,
    retained_tick: Any,
    selection: Any,
    session_box: Any,
    set_retained_tick: Any,
    terminal_host_box: Any,
    terminal_open: Any,
) -> Any:
    """Terminal lifecycle effects: revocation, route sync, pruning, phase watching, idle expiry."""

    def enforce_grants() -> None:
        # Revocation (per-host grant loss or global) closes the open session before it can write:
        # dispose stops renewal, unbinds the view and releases the lease. Unknown grants close
        # nothing.
        _purge_revoked_stash()
        if session_box.current is None or not terminal_open:
            return
        granted = app_state.granted_hosts
        if app_state.revoked or (granted is not None and terminal_host_box.current not in granted):
            close_terminal("revoke")

    ft.use_effect(enforce_grants, dependencies=[app_state, terminal_open])

    def sync_terminal_route() -> None:
        # The terminal is a detail of its route: navigating (or browser
        # history) away from the opened selection closes it; the remote
        # process survives and explicit Back/reopen resumes it.
        if terminal_open and (
            selection != terminal_open or destination not in ("agents", "terminals")
        ):
            close_terminal()

    ft.use_effect(sync_terminal_route, dependencies=[destination, selection, terminal_open])

    def prune_retained() -> None:
        current = controller.state
        allowed = {
            host.id: host.epoch
            for host in current.inventory.hosts.values()
            if not current.revoked
            and (current.granted_hosts is None or host.id in current.granted_hosts)
        }
        resources = {
            (agent.host_id, "agent", key) for key, agent in current.inventory.agents.items()
        } | {(tab.host_id, "shell", key) for key, tab in current.inventory.tabs.items()}
        for entry in retained.entries():
            if (
                allowed.get(entry.host_id) != entry.epoch
                or (entry.host_id, entry.kind, entry.key) not in resources
            ):
                entry.session.set_foreground(False)
        asyncio.create_task(retained.prune(allowed, resources))

    ft.use_effect(prune_retained, dependencies=[app_state])

    def reconcile_retained_view() -> None:
        if terminal_open and retained.foreground is None and session_box.current is not None:
            close_terminal()

    ft.use_effect(reconcile_retained_view, dependencies=[retained_tick, terminal_open])

    # Terminals list Live/Detached follows each retained shell's phase.
    # Sessions notify on every mutation; re-render only on a phase change.
    shell_phases: Any = ft.use_ref({})

    phase_watch: Any = ft.use_ref(())

    def watch_shell_phases() -> None:
        unsubscribes = []
        for entry in retained.entries():
            if entry.kind != "shell":
                continue

            def changed(session: Any, key: str = entry.key) -> None:
                phase = getattr(session, "phase", "")
                if shell_phases.current.get(key) != phase:
                    shell_phases.current[key] = phase
                    set_retained_tick(time.monotonic_ns())

            unsubscribes.append(entry.session.subscribe(changed))
        phase_watch.current = tuple(unsubscribes)

    def unwatch_shell_phases() -> None:
        for unsubscribe in phase_watch.current:
            unsubscribe()
        phase_watch.current = ()

    ft.use_effect(watch_shell_phases, dependencies=[retained_tick], cleanup=unwatch_shell_phases)

    def hold_terminal() -> None:
        async def expire() -> None:
            while True:
                await asyncio.sleep(15)
                await retained.evict_idle()

        expiry_task.current = asyncio.create_task(expire())

    def release_terminal() -> None:
        # Unmount (logout teardown re-renders past Root): never leave a
        # live session — or its stashed drafts — behind the login prompt.
        close_terminal("logout")
        if expiry_task.current is not None:
            expiry_task.current.cancel()
            expiry_task.current = None
        retained.changed = lambda: None

    ft.use_effect(hold_terminal, dependencies=[], cleanup=release_terminal)


def use_root_trace(
    *,
    retained: RetainedSessions,
    controller: Any,
    active: bool,
    wide: bool,
    dependencies: list[Any],
) -> None:
    """Trace `root_retained` only when its values change, not on every render."""
    last: Any = ft.use_ref(None)

    def trace() -> None:
        values = (id(retained), id(controller), len(retained.entries()), active, wide)
        if last.current == values:
            return
        last.current = values
        trace_lifecycle(
            "root_retained",
            owner=values[0],
            controller=values[1],
            entries=values[2],
            active=active,
            wide=wide,
        )

    ft.use_effect(trace, dependencies=dependencies)
