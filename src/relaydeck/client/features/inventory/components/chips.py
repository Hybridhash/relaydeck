"""Filter chips and dropdown helpers shared by the inventory list panes.

Plain functions returning the same controls `AppShell` used to build inline."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

import flet as ft

from relaydeck.client.components.design_system import HostChip
from relaydeck.client.features.inventory.selectors import space_rows
from relaydeck.client.features.terminal.events import event_text
from relaydeck.client.theme import MIN_TOUCH
from relaydeck.domain.models import AppState


def host_chips(
    state: AppState,
    pick: Callable[[str], Any],
    host: str,
    *,
    shells: bool = False,
) -> Any:
    """Horizontal host filter chips (`console-hosts` / `shells-hosts`)."""
    prefix = "shell-host-chip-" if shells else "host-chip-"
    return ft.Row(
        key="shells-hosts" if shells else "console-hosts",
        scroll=ft.ScrollMode.AUTO,
        spacing=8,
        controls=[
            HostChip("", "All hosts", not host, pick, key=f"{prefix}all", compact=True),
            *(
                HostChip(
                    value.id,
                    (value.label or value.id) + (" · Offline" if not value.reachable else ""),
                    host == value.id,
                    pick,
                    key=f"{prefix}{value.id}",
                    compact=True,
                )
                for value in state.inventory.hosts.values()
            ),
        ],
    )


def space_chips(
    state: AppState,
    pick_space: Callable[[str], Any],
    selected: str,
    host: str,
    *,
    shells: bool = False,
) -> Any:
    """Horizontal space filter chips; `selected` is the active space key."""
    prefix = "shell-space-chip-" if shells else "host-chip-space-"

    def chip(key: str, label: str, spoken: str) -> Any:
        active = selected == key
        button = HostChip(
            key, label, active, pick_space, key=f"{prefix}{key or 'all'}", compact=True
        )
        # Supply one complete name on the annotation; suppress only the
        # duplicate text label, preserving the native button action/focus.
        button.content.content.semantics_label = ""
        return ft.MergeSemantics(
            content=ft.Semantics(
                selected=active,
                label=f"{spoken}, {'selected' if active else 'not selected'}",
                content=button,
            )
        )

    noun = "terminal" if shells else "agent"
    return ft.Row(
        key="shells-spaces" if shells else "inventory-spaces",
        scroll=ft.ScrollMode.AUTO,
        spacing=8,
        controls=[
            chip("", "All spaces", "All spaces"),
            *(
                chip(
                    row.key,
                    f"{row.title} · {row.count}",
                    f"{row.title}, {row.count} {noun}{'' if row.count == 1 else 's'}",
                )
                for row in space_rows(state, host, shells=shells)
            ),
        ],
    )


def filter_field(
    key: str, label: str, value: str, options: list[tuple[str, str]], pick: Callable[[str], Any]
) -> Any:
    return ft.Dropdown(
        key=key,
        label=label,
        value=value,
        height=MIN_TOUCH,
        expand=True,
        options=[ft.DropdownOption(key=identifier, text=title) for identifier, title in options],
        on_select=lambda e: pick(event_text(e)),
    )


def filter_chip_row(hosts_row: Any, spaces_row: Any, *, show_hosts: bool) -> Any:
    """Hosts and spaces in ONE horizontally scrollable row.

    With a single host the host chips are not shown at all: a one-option filter only takes space.
    """
    controls = [*(hosts_row.controls if show_hosts else []), *spaces_row.controls]
    return ft.Row(
        key="console-filter-chips",
        scroll=ft.ScrollMode.AUTO,
        spacing=8,
        controls=controls,
    )
