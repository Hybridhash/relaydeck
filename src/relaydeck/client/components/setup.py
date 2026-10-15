"""Declarative setup diagnostics: pairing, sign-in and routing stay distinct."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from typing import Any

import flet as ft

from relaydeck.client.components.appearance import AppearanceBridge
from relaydeck.client.theme import palette


@ft.component
def SetupPanel(
    report: Callable[[], tuple[tuple[str, str], ...]],
    *,
    dark: bool = True,
    appearance: AppearanceBridge | None = None,
) -> Any:
    rows, set_rows = ft.use_state(report())
    dark, set_dark = ft.use_state(appearance.dark if appearance is not None else dark)
    unsubscribe: Any = ft.use_ref(None)
    pending: Any = ft.use_ref(None)
    colors = palette(dark)

    def start() -> None:
        if appearance is not None:
            set_dark(appearance.dark)
            unsubscribe.current = appearance.subscribe(set_dark)

        async def refresh() -> None:
            while True:
                await asyncio.sleep(1)
                set_rows(report())

        pending.current = asyncio.create_task(refresh())

    def cleanup() -> None:
        if pending.current is not None:
            pending.current.cancel()
        if unsubscribe.current is not None:
            unsubscribe.current()

    ft.use_effect(start, dependencies=[report, appearance], cleanup=cleanup)
    return ft.Container(
        key="companion-setup",
        padding=16,
        bgcolor=colors["surface"],
        content=ft.Column(
            controls=[
                ft.Text("Mac companion setup", weight=ft.FontWeight.BOLD),
                *[
                    ft.Column(
                        key=f"setup-{label}",
                        spacing=4,
                        controls=[
                            ft.Text(label, weight=ft.FontWeight.BOLD, color=colors["text"]),
                            ft.Text(value, selectable=True, color=colors["muted"]),
                        ],
                    )
                    for label, value in rows
                ],
            ],
        ),
    )
