"""Settings › Server health: request, error, loop-delay and socket tiles with 15-minute
sparklines, the slowest routes and live counts. Polls every 5 s only while it is showing and
the app is visible; shown only to a session that holds `server.health`."""

from __future__ import annotations

from typing import Any

import flet as ft

from relaydeck.client.components.design_system import tokens as T
from relaydeck.client.components.design_system.fonts import FAMILY_MONO
from relaydeck.client.components.design_system.primitives import text_style
from relaydeck.client.components.design_system.terminal_window import SettingsPanel
from relaydeck.client.features.settings.server_health_model import (
    REFUSED,
    HealthView,
    build_view,
    poll_loop,
)
from relaydeck.client.features.settings.server_health_parts import (
    TYPE_ROW,
    CountList,
    HealthTile,
    RefreshPill,
    RoutesTable,
    UpdatedAgo,
    tone_color,
)
from relaydeck.client.features.terminal.events import spawn

PANEL_KEY = "settings-server-health"
ERROR_TEXT = "Couldn't update · retrying"


def use_health_snapshot(transport: Any, active: bool) -> tuple[dict[str, Any] | None, str]:
    """(latest snapshot, last error code). The poll runs while `active`, one request at a time,
    and ends on hide, unmount or a refused scope; an error keeps the last snapshot."""
    initial: dict[str, Any] | None = None
    snapshot, set_snapshot = ft.use_state(initial)
    error, set_error = ft.use_state("")
    task: Any = ft.use_ref(None)
    available = callable(getattr(transport, "metrics", None))
    refused = error in REFUSED

    def start() -> None:
        if not (active and available and not refused):
            return

        def got(value: dict[str, Any]) -> None:
            set_snapshot(value)
            set_error("")

        task.current = spawn(
            poll_loop(transport.metrics, on_snapshot=got, on_error=set_error),
            label="server-health-poll",
        )

    def stop() -> None:
        if task.current is not None and not task.current.done():
            task.current.cancel()
        task.current = None

    ft.use_effect(start, dependencies=[active, id(transport), available, refused], cleanup=stop)
    return snapshot, error


def _status_row(view: HealthView, *, active: bool) -> Any:
    color = tone_color(view.status.tone)
    return ft.Row(
        key="health-status",
        spacing=8,
        wrap=True,
        vertical_alignment=ft.CrossAxisAlignment.CENTER,
        controls=[
            ft.Semantics(
                key="health-dot-semantics",
                label=f"Server status: {view.status.label}",
                content=ft.Container(
                    key="health-dot",
                    width=10,
                    height=10,
                    border_radius=ft.BorderRadius.all(5),
                    bgcolor=color,
                ),
            ),
            ft.Text(
                f"{view.status.label} · up {view.uptime} ·",
                key="health-status-text",
                style=text_style(FAMILY_MONO, TYPE_ROW, T.TEXT),
            ),
            UpdatedAgo(view.generated_ms, active=active),
        ],
    )


def _tiles(view: HealthView, phone: bool) -> Any:
    tiles = [HealthTile(tile) for tile in view.tiles]
    if phone:
        return ft.Column(
            key="health-tiles",
            spacing=8,
            controls=[
                ft.Row(spacing=8, controls=tiles[:2]),
                ft.Row(spacing=8, controls=tiles[2:]),
            ],
        )
    return ft.Row(key="health-tiles", spacing=12, controls=tiles)


def _side_lists(view: HealthView) -> Any:
    refresh = [RefreshPill(row) for row in view.refreshes]
    return ft.Column(
        key="health-lists",
        spacing=16,
        controls=[
            CountList("Live connections", view.connections, slug="connections"),
            CountList("Sign-ins", view.sign_ins, slug="signins"),
            ft.Column(
                key="health-refresh",
                spacing=4,
                controls=[
                    ft.Semantics(
                        header=True,
                        content=ft.Text(
                            "USAGE REFRESH",
                            style=text_style(FAMILY_MONO, TYPE_ROW, T.TEXT_MUTED, 0.08),
                        ),
                    ),
                    *(
                        refresh
                        or [
                            ft.Text(
                                "No tools configured",
                                style=text_style(FAMILY_MONO, TYPE_ROW, T.TEXT_MUTED),
                            )
                        ]
                    ),
                ],
            ),
        ],
    )


@ft.component
def HealthPanel(transport: Any, *, active: bool, phone: bool, zone: Any = None) -> Any:
    snapshot, error = use_health_snapshot(transport, active)
    if error in REFUSED or (snapshot is None and not error):
        return ft.Container(key="settings-server-health-hidden", visible=False)
    if snapshot is None:
        return SettingsPanel(
            "Server health",
            [
                ft.Text(
                    ERROR_TEXT,
                    key="health-error",
                    style=text_style(FAMILY_MONO, TYPE_ROW, T.TEXT_MUTED),
                )
            ],
            key=PANEL_KEY,
            phone=phone,
        )
    view = build_view(snapshot, zone)
    routes = RoutesTable(view.routes, phone=phone)
    lists = _side_lists(view)
    split: Any = (
        ft.Column(key="health-split", spacing=16, controls=[routes, lists])
        if phone
        else ft.Row(
            key="health-split",
            spacing=20,
            vertical_alignment=ft.CrossAxisAlignment.START,
            controls=[
                ft.Container(expand=2, content=routes),
                ft.Container(expand=1, content=lists),
            ],
        )
    )
    rows: list[Any] = [_status_row(view, active=active)]
    if error:
        rows.append(
            ft.Text(
                ERROR_TEXT,
                key="health-error",
                style=text_style(FAMILY_MONO, TYPE_ROW, tone_color("warn")),
            )
        )
    rows += [
        ft.Container(height=4),
        _tiles(view, phone),
        ft.Container(height=8),
        split,
        ft.Container(
            padding=ft.Padding.only(top=12, bottom=8),
            content=ft.Text(
                view.footer,
                key="health-footer",
                style=text_style(FAMILY_MONO, TYPE_ROW, T.TEXT_MUTED),
            ),
        ),
    ]
    return SettingsPanel("Server health", rows, key=PANEL_KEY, phone=phone)
