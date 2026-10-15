"""Read-only command reconciliation and explicit review of proven outcomes."""

from __future__ import annotations

import asyncio
from typing import Any

import flet as ft

from relaydeck.client.pending_commands import PendingCommands
from relaydeck.domain.models import scope_key

# pinned Flet has no max-height, so the opened review list
# sizes to content for 1-2 rows and only then takes the bounded 220px
# scrollable reservation (one accepted row no longer blanks the terminal;
# see rd-check-command-review.png).
_LIST_MAX_HEIGHT = 220
_NATURAL_ROWS = 2


@ft.component
def CommandRecovery(
    journal: PendingCommands, transport: Any, granted: frozenset[str], go: Any
) -> Any:
    _, refresh = ft.use_state(0)
    opened, set_opened = ft.use_state(False)
    notice, set_notice = ft.use_state("")
    unsub: Any = ft.use_ref(None)
    task: Any = ft.use_ref(None)
    alive: Any = ft.use_ref(True)

    def subscribe() -> None:
        alive.current = True
        unsub.current = journal.subscribe(lambda: refresh(lambda value: value + 1))

    def cleanup() -> None:
        alive.current = False
        if unsub.current is not None:
            unsub.current()
        if task.current is not None:
            task.current.cancel()

    ft.use_effect(subscribe, dependencies=[journal], cleanup=cleanup)

    def run(operation: Any) -> None:
        if task.current is not None and not task.current.done():
            operation.close()
            return

        async def work() -> None:
            try:
                await operation
                if alive.current:
                    set_notice("")
            except Exception:
                if alive.current:
                    set_notice("Status unavailable. No operation was resent.")

        task.current = asyncio.create_task(work())

    rows = list(journal.records.values())
    if not rows and not journal.error:
        return ft.Container(key="command-recovery-empty", height=0)
    controls: list[Any] = [
        ft.Row(
            wrap=True,
            controls=[
                ft.Text(f"Operations to review: {len(rows)}"),
                ft.Button(
                    "Hide" if opened else "Review",
                    key="command-recovery-toggle",
                    height=48,
                    on_click=lambda e: set_opened(not opened),
                ),
                ft.Button(
                    "Check status",
                    key="command-recovery-check",
                    height=48,
                    on_click=lambda e: run(journal.reconcile(transport.command_status, granted)),
                ),
            ],
        )
    ]
    if journal.error or notice:
        controls.append(ft.Text(journal.error or notice, key="command-recovery-error"))
    if opened:
        items: list[Any] = []
        for row in rows:
            host, command_id, created = row["host"], row["id"], row["created"]
            resource = (
                created.get("agent_id") or created.get("tab_id") or created.get("workspace_id")
            )
            destination = (
                "agents"
                if created.get("agent_id")
                else "terminals"
                if created.get("tab_id")
                else "workspaces"
            )
            actions: list[Any] = []
            if resource:
                route = f"/{destination}/{scope_key(host, resource)}"
                actions.append(
                    ft.Button(
                        "Open created resources",
                        height=48,
                        disabled=host not in granted,
                        on_click=lambda e, route=route: go(route),
                    )
                )
            if row["outcome"] in {"succeeded", "failed", "rejected"}:
                actions.append(
                    ft.Button(
                        "Reviewed",
                        height=48,
                        on_click=lambda e, host=host, command_id=command_id: run(
                            journal.acknowledge(host, command_id)
                        ),
                    )
                )
            items.append(
                ft.Column(
                    key=f"recovered-{host}-{command_id}",
                    controls=[
                        ft.Text(f"{row['kind']}: {row['outcome']}"),
                        ft.Text(
                            "Some resources were created; review them before a new creation.",
                            visible=bool(created and row["outcome"] != "succeeded"),
                        ),
                        ft.Row(wrap=True, controls=actions),
                    ],
                )
            )
        if len(items) <= _NATURAL_ROWS:
            controls.append(ft.Column(key="command-recovery-list", controls=items))
        else:
            controls.append(
                ft.Column(
                    key="command-recovery-list",
                    height=_LIST_MAX_HEIGHT,
                    scroll=ft.ScrollMode.AUTO,
                    controls=items,
                )
            )
    return ft.Container(key="command-recovery", padding=8, content=ft.Column(controls=controls))
