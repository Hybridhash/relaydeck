"""A native sheet, dialog or snack bar stays on screen when its control is removed while open."""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

import flet as ft
import pytest
from flet.messaging.session import Session

OVERLAYS = (ft.BottomSheet, ft.AlertDialog, ft.SnackBar)
GUARDED = ("test_agent_rename", "test_rename_redesign")


@pytest.fixture(autouse=True)
def no_open_overlay_is_removed(
    request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch
) -> Iterator[None]:
    if request.module.__name__.rsplit(".", 1)[-1] not in GUARDED:
        yield
        return
    original = Session._Session__get_update_control_patch  # type: ignore[attr-defined]
    violations: list[str] = []

    def watch(self: Any, *args: Any, **kwargs: Any) -> Any:
        result = original(self, *args, **kwargs)
        kept = {control._i for control in result[1]}
        for removed in result[2]:
            if isinstance(removed, OVERLAYS) and removed.open and removed._i not in kept:
                violations.append(f"{type(removed).__name__} {removed.key} removed while open")
        return result

    monkeypatch.setattr(Session, "_Session__get_update_control_patch", watch)
    yield
    assert not violations, violations
