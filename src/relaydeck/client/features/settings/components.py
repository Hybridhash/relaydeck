"""Settings page.

Web: panels in a two-column grid. Phone: the same groups as stacked
cards under small mono labels, without helper text.

Only rows the app honours are shown. Left out, with the reason:
- Terminal theme: the native terminal engine has no light palette yet.
- Host goes offline: no push is sent for it. Blocked / Done choices are
  per device (Settings › Notifications), stored on the companion.
- SSH key / Copy public key: devices pair with the Mac gateway, there
  is no per-device SSH key.
- Check for updates: there is no update channel.
Existing working controls (account, log out, devices and access,
notifications, diagnostics) keep their place below the four groups.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

import flet as ft

from relaydeck.client.components.design_system import tokens as T
from relaydeck.client.components.design_system.fonts import FAMILY_MONO
from relaydeck.client.components.design_system.primitives import SectionHeader, text_style
from relaydeck.client.components.design_system.terminal_window import (
    PillButton,
    SegmentedSwitch,
    SettingRow,
    SettingsPanel,
    Stepper,
    ToggleSwitch,
)
from relaydeck.client.features.settings.acknowledgements import ACKNOWLEDGEMENT_GROUPS
from relaydeck.client.features.settings.prefs import (
    FONT_MAX,
    FONT_MIN,
    FONT_STEP,
    TerminalPrefs,
)
from relaydeck.client.features.terminal.events import event_text
from relaydeck.client.features.terminal.input_modes import (
    WORKFLOW_AGENT_COMPOSER,
    WORKFLOW_AGENT_DIRECT,
)

APP_NAME = "RelayDeck"


def _size_label(size: float) -> str:
    return f"{size:g} px"


def AcknowledgementsList(*, phone: bool) -> Any:
    """Open-source packages and fonts with their licences, grouped."""
    rows: list[Any] = []
    for group in ACKNOWLEDGEMENT_GROUPS:
        rows.append(
            ft.Text(
                group.title.upper(),
                style=text_style(FAMILY_MONO, T.TYPE_META, T.TEXT_MUTED),
            )
        )
        rows.extend(
            ft.Row(
                alignment=ft.MainAxisAlignment.SPACE_BETWEEN,
                controls=[
                    ft.Text(entry.name, style=text_style(FAMILY_MONO, T.TYPE_META, T.TEXT)),
                    ft.Text(
                        entry.license,
                        style=text_style(FAMILY_MONO, T.TYPE_META, T.TEXT_MUTED),
                    ),
                ],
            )
            for entry in group.entries
        )
    return ft.Container(
        key="settings-acknowledgements-list",
        padding=ft.Padding.only(top=4, bottom=8),
        content=ft.Column(spacing=6 if phone else 8, controls=rows),
    )


@ft.component
def SettingsPage(
    prefs: TerminalPrefs,
    on_prefs: Callable[[TerminalPrefs], Any],
    *,
    phone: bool,
    wide: bool = True,
    account: str,
    version: str,
    on_logout: Callable[[], Any],
    scenarios: tuple[str, ...] = (),
    on_apply_scenario: Callable[[str], Any] | None = None,
    management: Any = None,
    hosts_section: Any = None,
    notifications_panel: Any = None,
    diagnostics: Any = None,
    saved: bool = True,
) -> Any:
    size = prefs.effective_font_size(phone)
    credits_open, set_credits_open = ft.use_state(False)

    def change(**fields: Any) -> None:
        from dataclasses import replace

        on_prefs(replace(prefs, **fields))

    help_ = (lambda text: "") if phone else (lambda text: text)
    appearance = SettingsPanel(
        "Appearance",
        [
            SettingRow(
                "Terminal text size",
                Stepper(
                    _size_label(size),
                    lambda: on_prefs(prefs.step_font(-FONT_STEP, phone)),
                    lambda: on_prefs(prefs.step_font(FONT_STEP, phone)),
                    key="settings-font-size",
                    can_minus=size > FONT_MIN,
                    can_plus=size < FONT_MAX,
                ),
                help_text=help_("Applies to terminals you open next"),
                first=True,
            )
        ],
        key="settings-appearance",
        phone=phone,
    )
    terminal_input = SettingsPanel(
        "Terminal input",
        [
            SettingRow(
                "Agents open in",
                SegmentedSwitch(
                    (
                        (WORKFLOW_AGENT_COMPOSER, "Compose", "settings-open-compose"),
                        (WORKFLOW_AGENT_DIRECT, "Direct", "settings-open-direct"),
                    ),
                    prefs.agents_open_in,
                    lambda value: change(agents_open_in=value),
                    key="settings-agents-open-in",
                    well=T.TERMINAL_BG,
                ),
                help_text=help_("Shells always open in Direct"),
                first=True,
            ),
            SettingRow(
                "Take control when opening an agent",
                ToggleSwitch(
                    prefs.auto_takeover,
                    lambda value: change(auto_takeover=value),
                    label="Take control automatically when opening an agent",
                    key="settings-auto-takeover",
                ),
                help_text=help_(
                    "Opening an agent connects in control; you are asked first if "
                    "another device has it"
                ),
            ),
            SettingRow(
                "Confirm before taking over",
                ToggleSwitch(
                    prefs.confirm_takeover,
                    lambda value: change(confirm_takeover=value),
                    label="Confirm before taking over a terminal",
                    key="settings-confirm-takeover",
                ),
                help_text=help_("Taking over disconnects another attached client"),
            ),
            SettingRow(
                "Show the key row",
                ToggleSwitch(
                    prefs.show_key_row,
                    lambda value: change(show_key_row=value),
                    label="Show the key row",
                    key="settings-show-key-row",
                ),
                help_text=help_("Esc, Tab, arrows and Enter under the terminal"),
            ),
            SettingRow(
                "Haptic feedback",
                ToggleSwitch(
                    prefs.haptics,
                    lambda value: change(haptics=value),
                    label="Haptic feedback",
                    key="settings-haptics",
                ),
                help_text=help_(
                    "Vibrate briefly on Send and Copy. Not available in iPhone/iPad browsers."
                ),
            ),
        ],
        key="settings-terminal-input",
        phone=phone,
    )
    about_rows: list[Any] = [
        SettingRow(
            APP_NAME,
            ft.Container(),
            help_text=f"Version {version}",
            mono_help=True,
            first=True,
            key="settings-version-row",
        ),
        SettingRow(
            "Signed in",
            PillButton("Log out", on_logout, key="settings-logout", tone="outline"),
            help_text=account or "This device",
            mono_help=True,
            key="settings-account",
        ),
        SettingRow(
            "Acknowledgements",
            PillButton(
                "Hide" if credits_open else "View",
                lambda: set_credits_open(not credits_open),
                key="settings-acknowledgements-toggle",
                tone="outline",
            ),
            help_text=help_("Open-source software and fonts"),
            key="settings-acknowledgements",
        ),
    ]
    if credits_open:
        about_rows.append(AcknowledgementsList(phone=phone))
    if scenarios and on_apply_scenario is not None:
        about_rows.append(
            SettingRow(
                "Fixture scenario",
                ft.Dropdown(
                    key="fixture-selector",
                    width=200,
                    options=[ft.DropdownOption(text=name, key=name) for name in scenarios],
                    on_select=lambda e: on_apply_scenario(event_text(e)),
                ),
                help_text=help_("Development only"),
            )
        )
    about = SettingsPanel(
        "Device and about" if not phone else "This device",
        about_rows,
        key="settings-about",
        phone=phone,
    )
    notifications = (
        SettingsPanel(
            "Notifications",
            [ft.Container(padding=ft.Padding.symmetric(vertical=8), content=notifications_panel)],
            key="settings-notifications",
            phone=phone,
        )
        if notifications_panel is not None
        else None
    )
    extras: list[Any] = []
    if hosts_section is not None:
        extras.append(
            SettingsPanel(
                "Hosts",
                [ft.Container(padding=ft.Padding.only(top=4, bottom=8), content=hosts_section)],
                key="settings-hosts",
                phone=phone,
            )
        )
    if management is not None:
        extras.append(
            SettingsPanel(
                "Devices and access",
                [ft.Container(padding=ft.Padding.only(top=4, bottom=8), content=management)],
                key="settings-management",
                phone=phone,
            )
        )
    if diagnostics is not None:
        extras.append(
            SettingsPanel(
                "Diagnostics",
                [
                    SettingRow(
                        label,
                        ft.Text(
                            str(value),
                            key=f"diagnostics-{label}",
                            style=text_style(FAMILY_MONO, T.TYPE_META, T.TEXT_MUTED),
                            text_align=ft.TextAlign.RIGHT,
                            selectable=True,
                        ),
                        first=index == 0,
                    )
                    for index, (label, value) in enumerate(diagnostics)
                ],
                key="diagnostics-pane",
                phone=phone,
            )
        )
    header = SectionHeader(
        (f"{APP_NAME} · version {version}" if phone else "Saved on this device").upper()
        if saved
        else "NOT SAVED · THIS SESSION ONLY",
        "Settings",
    )
    # The four design groups. Wide: two independent columns so each stacks
    # tightly (a row-by-row grid left gaps beside short panels). Narrower:
    # one column. Devices and diagnostics follow at full width.
    # Left column = the first two groups, right = the rest, so keyboard
    # order matches the single-column order at every width.
    left = [appearance, terminal_input]
    right = [*([notifications] if notifications is not None else []), about]
    gap = 12 if phone else 20
    if wide and not phone:
        body: Any = ft.Column(
            spacing=gap,
            horizontal_alignment=ft.CrossAxisAlignment.STRETCH,
            controls=[
                ft.Row(
                    spacing=gap,
                    vertical_alignment=ft.CrossAxisAlignment.START,
                    controls=[
                        ft.Column(expand=1, spacing=gap, controls=left),
                        ft.Column(expand=1, spacing=gap, controls=right),
                    ],
                ),
                *extras,
            ],
        )
    else:
        body = ft.Column(
            spacing=gap,
            horizontal_alignment=ft.CrossAxisAlignment.STRETCH,
            controls=[*left, *right, *extras],
        )
    return ft.Column(
        key="settings-pane",
        expand=True,
        scroll=ft.ScrollMode.AUTO,
        spacing=20 if not phone else 12,
        controls=[header, body],
    )


__all__ = ["APP_NAME", "AcknowledgementsList", "SettingsPage"]
