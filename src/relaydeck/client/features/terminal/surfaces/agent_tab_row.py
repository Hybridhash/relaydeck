"""Agent tabs with a foreground-only working indicator.

One shared timer runs only while a working tab is visible. Local blink state
rerenders this row without disturbing the composer."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from typing import Any

import flet as ft

from relaydeck.client.features.terminal.events import click

# The working dot toggles between full and dim opacity every WORKING_BLINK_S.
WORKING_BLINK_S = 0.6
DIM_OPACITY = 0.3


async def _blink(set_lit: Callable[[bool], Any]) -> None:
    """Toggles the working dot until cancelled. One task per row, never one per button."""
    lit = True
    while True:
        await asyncio.sleep(WORKING_BLINK_S)
        lit = not lit
        set_lit(lit)


@ft.component
def AgentTabRow(
    *,
    agent_tabs: tuple[tuple[str, str, str], ...],
    agent_status: str,
    agent_title: str,
    on_agent_tab: Any,
    selected_agent: str,
    tokens: dict[str, str],
    visible: bool,
    blink: bool = False,
) -> Any:
    """One tab button per agent of the host (or just the current one).

    `blink` is the caller's visibility signal: the agent screen passes
    `session.foreground and not session.paused`. A parked or backgrounded session passes
    False, so its dock never blinks. No page handler is installed here.
    """
    lit, set_lit = ft.use_state(True)
    blink_task: Any = ft.use_ref(None)
    tabs = agent_tabs or ((selected_agent, agent_title, agent_status),)
    active = blink and any(status == "working" for _, _, status in tabs)

    def start_blink() -> None:
        if active:
            set_lit(True)
            blink_task.current = asyncio.create_task(_blink(set_lit))

    def stop_blink() -> None:
        if blink_task.current is not None:
            blink_task.current.cancel()
            blink_task.current = None

    ft.use_effect(start_blink, dependencies=[active], cleanup=stop_blink)
    dim = active and not lit
    controls: list[Any] = []
    for identifier, label, status in tabs:
        working = status == "working"
        text = ft.Text(label, size=13, no_wrap=True)
        content: Any = text
        if working:
            dot = ft.Container(
                key=f"terminal-agent-working-dot-{identifier}",
                width=8,
                height=8,
                border_radius=4,
                # Palette green for "success"/live (theme.py: light "success", dark DONE).
                bgcolor=tokens["success"],
                opacity=DIM_OPACITY if dim else 1.0,
                animate_opacity=ft.Animation(600, ft.AnimationCurve.EASE_IN_OUT),
            )
            content = ft.Row(
                [dot, text],
                spacing=6,
                tight=True,
                vertical_alignment=ft.CrossAxisAlignment.CENTER,
            )
        controls.append(
            ft.TextButton(
                key=f"terminal-agent-tab-{identifier}",
                height=30,
                tooltip=f"{label}, working" if working else None,
                content=content,
                style=ft.ButtonStyle(
                    padding=ft.Padding.symmetric(horizontal=8),
                    bgcolor=tokens["raised"]
                    if identifier == selected_agent
                    else ft.Colors.TRANSPARENT,
                ),
                on_click=click(
                    lambda identifier=identifier: on_agent_tab(identifier) if on_agent_tab else None
                ),
            )
        )
    return ft.Row(
        key="agent-tabs",
        visible=visible,
        height=36,
        scroll=ft.ScrollMode.HIDDEN,
        spacing=6,
        controls=controls,
    )
