"""Transient authority and parked presentation records for Direct typing."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

import flet as ft

from relaydeck.client.features.terminal.input_modes import ROUTE_DROPPED

if TYPE_CHECKING:
    from relaydeck.client.features.terminal.surfaces.actions import DirectInput


def _ignore_revision(_value: Any) -> None:
    """Ignore updates from a retired native field."""


async def _drop_input(_origin: int, _data: bytes) -> str:
    return ROUTE_DROPPED


@dataclass
class _DirectCallbacks:
    """Mount-owned callbacks; retired native epochs must not own the parent tree."""

    set_revision: Callable[[Any], None] = _ignore_revision
    on_input: Callable[[int, bytes], Awaitable[str]] = _drop_input
    on_transaction: DirectInput | None = None
    can_input: Callable[[], bool] | None = None
    on_input_handled: Callable[[], None] | None = None
    on_focus: Callable[[], Any] | None = None

    def allowed(self) -> bool:
        return self.can_input is None or self.can_input()

    def revise(self) -> None:
        self.set_revision(lambda current: current + 1)

    def handled(self) -> None:
        if self.on_input_handled is not None:
            self.on_input_handled()


@dataclass
class _ParkedEditor:
    """Uncommitted display; authority changes rebase restored characters as unsent."""

    value: str
    selection: ft.TextSelection


@dataclass(frozen=True)
class _Authority:
    owner: int
    writer: tuple[Any, ...]
    foreground: bool
    interaction: int

    @classmethod
    def read(cls, binding: Any) -> _Authority | None:
        owner = binding[0] if isinstance(binding, tuple) and binding else None
        if owner is None or not hasattr(owner, "interaction_generation"):
            return None
        controller, lease = owner.controller, owner.lease
        return cls(
            id(owner),
            (
                id(controller),
                getattr(controller, "generation", None),
                getattr(controller, "mode", None),
                getattr(controller, "writer_revoked", None),
                getattr(lease, "lease_id", None),
                getattr(lease, "epoch", None),
                getattr(lease, "generation", None),
            ),
            owner.foreground,
            owner.interaction_generation,
        )

    def navigation_from(self, before: _Authority | None) -> bool:
        return (
            before is not None
            and self.owner == before.owner
            and self.writer == before.writer
            and self.foreground != before.foreground
            and self.interaction == before.interaction + 1
        )
