"""Status signals.

StatusDot moved here verbatim from inventory components (same keys and
behavior); StatusWord pairs the dot with its word for host-context and
list headers. Dots always pair with text labels (never color alone).
"""

from __future__ import annotations

from typing import Any

import flet as ft

from relaydeck.client.theme import STATUS_LABELS, status_color

__all__ = ["StatusDot", "StatusWord"]


@ft.component
def StatusDot(status: str, dark: bool = True) -> Any:
    return ft.Container(
        key=f"dot-{status}",
        width=12,
        height=12,
        border_radius=6,
        bgcolor=status_color(status, dark),
        tooltip=STATUS_LABELS.get(status, status),
    )


@ft.component
def StatusWord(status: str, dark: bool = True) -> Any:
    """Non-interactive dot + status word (reference `.status` row)."""
    label = STATUS_LABELS.get(status, status)
    return ft.Row(
        key=f"status-word-{status}",
        spacing=8,
        controls=[
            ft.Container(
                key=f"status-word-{status}-dot",
                width=6,
                height=6,
                border_radius=3,
                bgcolor=status_color(status, dark),
            ),
            ft.Text(label, key=f"status-word-{status}-label", color=status_color(status, dark)),
        ],
    )
