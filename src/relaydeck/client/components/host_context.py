"""Persistent host-context header.

Reference `.host` block: current host label plus a reachability word.
Non-interactive by default so it never disturbs the declared keyboard
focus order; the optional ``on_open`` adds one trailing "Switch" action
driven by the existing host-selection callback shape.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

import flet as ft

from relaydeck.client.theme import MIN_TOUCH, palette, status_color

__all__ = ["HostContextHeader"]


@ft.component
def HostContextHeader(
    host_label: str,
    status_text: str,
    reachable: bool,
    on_open: Callable[[], Any] | None = None,
    dark: bool = True,
) -> Any:
    tokens = palette(dark)
    dot = status_color("done" if reachable else "unknown", dark)
    header: list[Any] = [
        ft.Row(
            key="host-context-title",
            spacing=8,
            controls=[
                ft.Icon(ft.Icons.DNS, key="host-context-icon", color=tokens["muted"]),
                ft.Text(
                    host_label or "No host",
                    key="host-context-label",
                    color=tokens["text"],
                    expand=True,
                    max_lines=1,
                    overflow=ft.TextOverflow.ELLIPSIS,
                    tooltip=host_label or "No host",
                ),
            ],
        ),
        ft.Row(
            key="host-context-status",
            spacing=8,
            controls=[
                ft.Container(
                    key="host-context-dot",
                    width=6,
                    height=6,
                    border_radius=3,
                    bgcolor=dot,
                ),
                ft.Text(status_text, key="host-context-status-text", color=tokens["muted"]),
            ],
        ),
    ]
    if on_open is not None:
        header.append(
            ft.TextButton(
                "Switch",
                key="host-context-switch",
                height=MIN_TOUCH,
                tooltip="Switch host",
                on_click=lambda e: on_open(),
            )
        )
    return ft.Column(
        key="host-context",
        spacing=4,
        controls=[
            ft.Container(
                key="host-context-body",
                bgcolor=tokens["surface"],
                padding=12,
                content=ft.Column(spacing=4, controls=header),
            ),
            ft.Divider(key="host-context-divider", height=1, color=tokens["line"]),
        ],
    )
