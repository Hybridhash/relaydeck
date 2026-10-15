"""Declarative workspace shell chooser; mutations belong to Root."""

from collections.abc import Callable
from typing import Any

import flet as ft

from relaydeck.client.components.appearance import chrome_style
from relaydeck.client.features.terminal.events import click
from relaydeck.client.theme import MIN_TOUCH, palette
from relaydeck.domain.models import Tab, scope_key


@ft.component
def ShellChooser(
    tabs: tuple[Tab, ...],
    on_choose: Callable[[str], Any],
    on_new: Callable[[], Any],
    on_cancel: Callable[[], Any],
    dark: bool = True,
) -> Any:
    tokens = palette(dark)
    return ft.Column(
        key="shell-chooser",
        controls=[
            ft.Text("Open shell terminal", key="shell-chooser-title", color=tokens["text"]),
            ft.Text("Choose a terminal in this workspace.", color=tokens["muted"]),
            *[
                ft.Button(
                    tab.label or tab.id,
                    key=f"shell-choice-{scope_key(tab.host_id, tab.id)}",
                    height=MIN_TOUCH,
                    style=chrome_style(dark),
                    on_click=click(on_choose, scope_key(tab.host_id, tab.id)),
                )
                for tab in tabs
            ],
            ft.Button(
                "New terminal",
                key="shell-choice-new",
                height=MIN_TOUCH,
                style=chrome_style(dark),
                on_click=lambda e: on_new(),
            ),
            ft.Button(
                "Cancel",
                key="shell-choice-cancel",
                height=MIN_TOUCH,
                style=chrome_style(dark),
                on_click=lambda e: on_cancel(),
            ),
        ],
    )
