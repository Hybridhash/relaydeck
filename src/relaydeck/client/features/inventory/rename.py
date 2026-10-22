"""Explicit name changes through the journaled gateway command path."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import uuid4

import flet as ft

from relaydeck.client.features.composer.observation import (
    CommandObserver,
    CommandOutcomeUnknown,
    CommandRefused,
)
from relaydeck.client.features.inventory import rename_model as model
from relaydeck.client.features.inventory.rename_presentation import use_presentation
from relaydeck.client.features.inventory.rename_sheet import (
    RenameSheetView,
    SheetActions,
    SheetContext,
    SheetProps,
)
from relaydeck.client.features.sync.transport import TransportError
from relaydeck.contracts.launch import display_name_clean
from relaydeck.contracts.schemas import GATEWAY_PROTOCOL, validate
from relaydeck.domain.models import AppState


def rename_command(host_id: str, host_epoch: str, agent_id: str, name: str) -> dict[str, Any]:
    return {
        "protocol": GATEWAY_PROTOCOL,
        "command_id": str(uuid4()),
        "host_id": host_id,
        "host_epoch": host_epoch,
        "kind": "agent.rename",
        "target": {"agent_id": agent_id},
        "payload": {"name": name},
        "expires_at": (datetime.now(UTC) + timedelta(seconds=120)).strftime("%Y-%m-%dT%H:%M:%SZ"),
    }


@ft.component
def RenameDialog(
    agent_key: str,
    initial_name: str,
    state: AppState,
    transport: Any,
    on_close: Callable[[], Any],
    on_refresh: Callable[[], Any] | None = None,
    dark: bool = True,
    foreground: bool = True,
    context: SheetContext | None = None,
    taken: tuple[str, ...] = (),
    phone: bool = False,
    preset: str | None = None,
    on_pending: Callable[[str], Any] | None = None,
    on_result: Callable[[str, bool], Any] | None = None,
    on_abandon: Callable[[str], Any] | None = None,
    leaving: bool = False,
) -> Any:
    """One rename: the sheet, and the command it sends. `preset` renames without showing the
    sheet (Undo, Retry); the sheet appears only if that run fails."""
    quiet = preset is not None
    value, set_value = ft.use_state(initial_name if preset is None else preset)
    first_render = ft.use_ref(True)
    select_all = first_render.current
    first_render.current = False
    error, set_error = ft.use_state("")
    status, set_status = ft.use_state("")
    busy, set_busy = ft.use_state(False)
    uncertain, set_uncertain = ft.use_state(False)
    live = ft.use_ref(True)
    task: Any = ft.use_ref(None)
    command: Any = ft.use_ref(None)
    latest: Any = ft.use_ref(None)
    closer: Any = ft.use_ref(None)
    latest.current = (state, transport, foreground)
    agent = state.inventory.agents.get(agent_key)
    host = state.inventory.hosts.get(agent.host_id) if agent else None
    identity = ft.use_ref(
        (agent.host_id, host.epoch, agent.terminal_id) if agent and host else None
    )

    def current(*, observing: bool = False) -> bool:
        model, adapter, shown = latest.current
        row = model.inventory.agents.get(agent_key)
        current_host = model.inventory.hosts.get(row.host_id) if row else None
        return bool(
            live.current
            and shown
            and adapter is transport
            and not model.revoked
            and row
            and current_host
            and identity.current is not None
            and (row.host_id, row.terminal_id) == (identity.current[0], identity.current[2])
            and (observing or current_host.epoch == identity.current[1])
            and (model.granted_hosts is None or row.host_id in model.granted_hosts)
        )

    def dispose() -> None:
        live.current = False
        if task.current is not None:
            task.current.cancel()

    ft.use_effect(lambda: None, dependencies=[], cleanup=dispose)
    space = context.space if context else ""
    verdict = model.validate(value, initial_name, taken, space)
    suggestions = (
        model.suggestions(context.task, space, initial_name, taken) if context and not quiet else ()
    )

    def submit(*, check_only: bool = False) -> None:
        if task.current is not None or not current(observing=check_only):
            return
        if not check_only and not (quiet or verdict.saveable):
            return
        if not check_only:
            if identity.current is None:
                return
            set_uncertain(False)
            command.current = rename_command(*identity.current, display_name_clean(value))
        body = command.current
        if body is None:
            return
        sent_name = body["payload"]["name"]
        settled = [False]
        if not check_only and on_pending is not None:
            on_pending(sent_name)

        def check_record(record: Any) -> None:
            validate("relaydeck/gateway/v1/command-record", record)
            if any(
                record.get(key) != body[key] for key in ("command_id", "host_id", "kind", "target")
            ):
                raise CommandOutcomeUnknown("Command identity changed.")

        async def status_record(host_id: str, command_id: str) -> dict[str, Any] | None:
            while current(observing=True):
                try:
                    record = await transport.command_status(host_id, command_id)
                except TransportError as exc:
                    if not exc.retryable:
                        raise
                    await asyncio.sleep(0.2)
                    continue
                check_record(record)
                return record  # type: ignore[no-any-return]
            raise CommandOutcomeUnknown("Command observation is no longer current.")

        async def run() -> None:
            set_busy(True)
            set_error("")
            set_status("Checking status…" if check_only else "Sending…")
            try:
                if not current(observing=check_only):
                    return
                submitted = None
                if not check_only:
                    submitted = await transport.submit_command(body)
                    check_record(submitted)
                    if not current():
                        if current(observing=True):
                            set_status("Outcome unknown.")
                            set_error("The Mac reconnected. Check status before trying again.")
                            set_uncertain(True)
                        return
                if not current(observing=True):
                    return
                set_status("Waiting for the host outcome…")
                if submitted is not None and submitted["status"] in ("failed", "rejected"):
                    raise CommandRefused(submitted.get("error_code", "command_rejected"))
                if submitted is None or submitted["status"] != "succeeded":
                    await CommandObserver(status_record).observe(
                        body["host_id"], body["command_id"]
                    )
                if current(observing=True):
                    settled[0] = True
                    if on_refresh is not None:
                        on_refresh()
                    if on_result is not None:
                        on_result(sent_name, True)
                    closer.current()
            except CommandRefused as exc:
                if current(observing=True):
                    set_status("Host refused the rename.")
                    set_error(
                        {
                            "invalid_agent_name": "Herdr refused this name",
                            "agent_name_taken": "Another agent already uses this name.",
                            "agent_not_found": "This agent is no longer available.",
                            "stale_epoch": "The Mac reconnected. Check status before trying again.",
                            "unsupported_capability": "This Mac does not support renaming agents.",
                            "internal": "Something went wrong on the Mac.",
                        }.get(exc.code, "The Mac refused the rename. Refresh before trying again.")
                    )
                    set_uncertain(False)
            except TransportError as exc:
                if current(observing=True):
                    refused = not exc.retryable and exc.code in {
                        "unauthenticated",
                        "forbidden",
                        "not_found",
                        "unsupported_capability",
                        "stale_epoch",
                        "stale_revision",
                        "command_conflict",
                        "command_recovery",
                    }
                    set_status("Rename refused." if refused else "Outcome unknown.")
                    set_error(
                        "Refresh and check access before trying again."
                        if refused
                        else "Check status or explicitly rename again."
                    )
                    set_uncertain(not refused)
            except Exception:
                if current(observing=True):
                    set_status("Outcome unknown.")
                    set_error("Check status or explicitly rename again.")
                    set_uncertain(True)
            finally:
                task.current = None
                if not settled[0]:
                    if live.current and on_result is not None:
                        on_result(sent_name, False)
                    elif not live.current and on_abandon is not None:
                        on_abandon(sent_name)
                if live.current and not settled[0]:  # a settled sheet is closing; never reopen it
                    set_busy(False)
                    if not current(observing=True):
                        set_status("Outcome unknown.")
                        set_error("Check status before trying again.")
                        set_uncertain(True)

        task.current = asyncio.create_task(run())

    ft.use_effect(lambda: submit() if quiet else None, dependencies=[])

    wanted = (not busy and not quiet) or bool(error)
    presentation = use_presentation(wanted, on_close)
    closer.current = presentation.close
    ft.use_effect(lambda: presentation.close() if leaving else None, dependencies=[leaving])

    props = SheetProps(
        open=presentation.open,
        phone=phone,
        dark=dark,
        value=value,
        context=context or SheetContext("", "", "", "", "#A9A2C8"),
        verdict=verdict,
        error=error,
        status=status,
        busy=busy,
        uncertain=uncertain,
        can_save=not busy and (quiet or verdict.saveable) and current(),
        save_label="Rename again" if uncertain else "Save",
        select_all=bool(select_all),
        suggestions=suggestions,
    )
    actions = SheetActions(
        on_change=lambda text: (set_value(model.clamp(text)), set_error("")),
        on_save=submit,
        on_cancel=presentation.close,
        on_dismiss=presentation.dismissed,
        on_check=lambda: submit(check_only=True),
        on_pick=lambda name: (set_value(model.clamp(name)), set_error("")),
    )
    return ft.Column(
        key=f"agent-rename-presentation-{presentation.generation}",
        spacing=0,
        tight=True,
        controls=[RenameSheetView("agent-rename-dialog", props, actions)],
    )
