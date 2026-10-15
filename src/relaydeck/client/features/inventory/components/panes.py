"""Detail placeholder and the light-theme settings / diagnostics panes
(components, unchanged from the single module)."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

import flet as ft

from relaydeck.client.components.appearance import chrome_style
from relaydeck.client.components.design_system import tokens as T
from relaydeck.client.components.design_system.terminal_window import (
    PillButton,
    TerminalHeader,
    WindowFrame,
)
from relaydeck.client.components.status import StatusDot
from relaydeck.client.features.inventory.selectors import DiagnosticsModel
from relaydeck.client.features.terminal.events import event_text
from relaydeck.client.theme import MIN_TOUCH, palette


@ft.component
def SessionDetail(
    title: str,
    subtitle: str,
    status: str,
    on_open_terminal: Callable[[], Any],
    missing: bool = False,
    on_back: Callable[[], Any] | None = None,
    dark: bool = True,
    on_open_shell: Callable[[], Any] | None = None,
    empty_title: str = "Your agents, together",
    empty_hint: str = "Select a session to see details.",
    open_label: str = "Show terminal",
) -> Any:
    tokens = palette(dark)
    if not title:
        if missing:
            return ft.Text(
                "Session unavailable. It may have been closed or revoked.",
                key="detail-missing",
            )
        return ft.Container(
            expand=True,
            bgcolor=tokens["surface"],
            border=ft.Border.all(1, tokens["line"]),
            border_radius=T.RADIUS_PANE,
            padding=24,
            alignment=ft.Alignment.CENTER,
            content=ft.Column(
                tight=True,
                horizontal_alignment=ft.CrossAxisAlignment.CENTER,
                spacing=12,
                controls=[
                    ft.Icon(ft.Icons.TERMINAL, size=40, color=tokens["accent_text"]),
                    ft.Text(
                        empty_title,
                        size=22,
                        weight=ft.FontWeight.W_700,
                        color=tokens["text"],
                    ),
                    ft.Text(
                        empty_hint,
                        key="detail-empty",
                        color=tokens["muted"],
                    ),
                ],
            ),
        )
    if dark:
        # Selection opens the terminal window straight away;
        # this frame only shows while it mounts, or for a deep-linked
        # shell, where attaching still needs an explicit press.
        return WindowFrame(
            [
                TerminalHeader(
                    title=title,
                    host_line=subtitle,
                    pills=[],
                    actions=[],
                    on_back=on_back,
                    title_key="detail-title",
                    key="detail-header",
                    back_key="detail-back",
                    phone=on_back is not None,
                ),
                ft.Container(
                    key="detail-opening",
                    expand=True,
                    border_radius=18,
                    bgcolor=tokens["terminal_bg"],
                    border=ft.Border.all(1, tokens["line"]),
                    alignment=ft.Alignment.CENTER,
                    content=ft.Column(
                        tight=True,
                        horizontal_alignment=ft.CrossAxisAlignment.CENTER,
                        spacing=14,
                        controls=[
                            ft.Text(
                                "Opening attaches this device to the terminal.",
                                key="detail-subtitle",
                                color=tokens["terminal_muted"],
                                size=13,
                            ),
                            PillButton(
                                open_label, on_open_terminal, key="detail-open", tone="primary"
                            ),
                        ],
                    ),
                ),
            ],
            key="session-detail-frame",
            phone=on_back is not None,
        )
    controls: list[Any] = [
        ft.Row(
            controls=[
                StatusDot(status, dark=dark),
                ft.Text(
                    title,
                    key="detail-title",
                    color=tokens["text"],
                    size=22,
                    weight=ft.FontWeight.W_700,
                    expand=True,
                    max_lines=2,
                    overflow=ft.TextOverflow.ELLIPSIS,
                    tooltip=title,
                ),
            ]
        ),
        ft.Text(subtitle, key="detail-subtitle", color=tokens["muted"]),
        ft.Button(
            "Open terminal",
            key="detail-open",
            on_click=lambda e: on_open_terminal(),
            height=MIN_TOUCH,
            style=chrome_style(dark),
        ),
    ]
    if on_open_shell is not None:
        controls.append(
            ft.Button(
                "Open shell terminal",
                key="detail-open-shell",
                height=MIN_TOUCH,
                style=chrome_style(dark),
                on_click=lambda e: on_open_shell(),
            )
        )
    if on_back is not None:
        controls.insert(
            0,
            ft.Button("Back", key="detail-back", height=MIN_TOUCH, on_click=lambda e: on_back()),
        )
    return ft.Container(
        expand=True,
        padding=20,
        border_radius=T.RADIUS_PANE,
        bgcolor=tokens["surface"],
        border=ft.Border.all(1, tokens["line"]),
        content=ft.Column(controls=controls, expand=True, spacing=14),
    )


@ft.component
def DiagnosticsPane(model: DiagnosticsModel) -> Any:
    rows = (
        ("Connection", model.connection),
        ("Gateway", "reachable" if model.gateway_reachable else "unreachable"),
        ("Sync socket", model.socket_status),
        ("Host", model.host_label or "none"),
        ("Host last seen", model.host_last_seen),
        ("Host protocol", model.host_protocol),
        ("Cursor", model.cursor_preview),
        ("Correlation", model.correlation_preview),
        ("App", model.app_version),
    )
    return ft.Column(
        controls=[
            ft.Text("Diagnostics", key="diagnostics-title"),
            *[ft.Text(f"{label}: {value}", key=f"diagnostics-{label}") for label, value in rows],
        ]
    )


@ft.component
def SettingsPane(
    account: str,
    version: str,
    scenarios: tuple[str, ...],
    on_apply_scenario: Callable[[str], Any],
    on_logout: Callable[[], Any],
) -> Any:
    controls: list[Any] = [
        ft.Text(account or "Account", key="settings-account"),
        ft.Button(
            "Log out", key="settings-logout", height=MIN_TOUCH, on_click=lambda e: on_logout()
        ),
        ft.Text(f"RelayDeck {version}", key="settings-version"),
    ]
    if scenarios:
        controls.append(
            ft.Dropdown(
                key="fixture-selector",
                label="Fixture scenario (dev only)",
                options=[ft.DropdownOption(text=name, key=name) for name in scenarios],
                on_select=lambda e: on_apply_scenario(event_text(e)),
            )
        )
    return ft.Column(controls=controls)
