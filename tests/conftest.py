"""Suite-wide test defaults.

Opening an agent takes control automatically in the product (Settings: "Take
control when opening an agent"). Most UI tests pin the watch-first contract
(open, watch, explicit Take over), so the automatic step is a no-op unless a test
opts in with ``@pytest.mark.auto_takeover``. The automatic path itself is covered
by tests that call it directly or opt in.

A newly opened terminal also holds its size reports while its layout settles. Tests that drive a
terminal right after opening it release that hold at once unless they opt in with
``@pytest.mark.open_hold``; the hold itself has its own tests.
"""

from __future__ import annotations

import os
from typing import Any

import pytest

# The theme is chosen once at import from the date; pin the default so tests that compare
# colours do not change during a seasonal window. Overlay tests resolve themes explicitly.
os.environ.setdefault("RELAYDECK_THEME", "default")


@pytest.fixture(autouse=True)
def _watch_first_unless_opted_in(request: pytest.FixtureRequest, monkeypatch: Any) -> None:
    if request.node.get_closest_marker("auto_takeover") is not None:
        return

    async def watch_first(session: Any) -> None:
        return None

    monkeypatch.setattr("relaydeck.client.hooks.actions._auto_take_control", watch_first)


@pytest.fixture(autouse=True)
def _open_hold_released_unless_opted_in(request: pytest.FixtureRequest, monkeypatch: Any) -> None:
    if request.node.get_closest_marker("open_hold") is not None:
        return
    monkeypatch.setattr("relaydeck.client.features.terminal.surfaces.agent_hooks.OPEN_HOLD_S", 0.0)
