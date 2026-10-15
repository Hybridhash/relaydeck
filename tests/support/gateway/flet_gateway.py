"""Gateway state for Flet-served browser sessions."""

from __future__ import annotations

from relaydeck.gateway.app import GatewayState
from relaydeck.gateway.backend import FakeBackend


def _gateway_state() -> GatewayState:
    return GatewayState(
        backend=FakeBackend(now_ms=lambda: 1_000_000),
        cursor_secret=b"x" * 32,
        now_ms=lambda: 1_000_000,
    )
