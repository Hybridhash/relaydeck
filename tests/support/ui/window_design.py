"""Terminal window design checks."""

from __future__ import annotations

from typing import Any

import flet as ft

from relaydeck.client.features.a11y.focus import iter_controls


def _missing(app: Any, key: str) -> bool:
    try:
        app.find(key)
    except KeyError:
        return True
    return False


def _wrap_violations(app: Any) -> list[str]:
    """In Flet 1.0.3 a Wrap (Row wrap=True) directly inside a plain Row gets
    unbounded width, and design-system buttons inside a Wrap misbehave; both
    crashed the web client into Flutter's grey error box."""
    problems: list[str] = []
    for control in iter_controls([app.page.views[0].controls]):
        if not isinstance(control, ft.Row):
            continue
        for kid in control.controls or []:
            if isinstance(kid, ft.Row) and kid.wrap and not control.wrap:
                problems.append(f"wrap row {kid.key!r} inside plain row {control.key!r}")
            if control.wrap and isinstance(kid, ft.TextButton):
                problems.append(f"button {kid.key!r} directly inside wrap row {control.key!r}")
            if control.wrap and getattr(kid, "expand", None):
                problems.append(f"expanded child {kid.key!r} inside wrap row {control.key!r}")
    return problems
