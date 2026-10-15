"""Q6b: `render_component` must keep a session's scheduler task alive.

Flet's `Session.__updates_scheduler` task is referenced only by a cycle through its own
session. When a test drops its `HostApp` mid-run (for example by reassigning `app` in a
loop), the cyclic GC can destroy that task while it is still pending, which asyncio logs as
"Task was destroyed but it is pending!" at an arbitrary later point, failing whichever test
happens to run the collection. The harness therefore keeps every session alive until its
event loop is closed.
"""

from __future__ import annotations

import asyncio
import gc
import logging

import flet as ft
import pytest

from tests.support import host_harness
from tests.support.host_harness import render_component, run


def _destroyed(caplog: pytest.LogCaptureFixture) -> list[str]:
    return [
        r.getMessage() for r in caplog.records if "destroyed but it is pending" in r.getMessage()
    ]


def test_a_dropped_host_app_does_not_lose_its_scheduler_task(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.ERROR, logger="asyncio")

    async def scenario() -> None:
        for _ in range(3):
            app = await render_component(lambda: ft.Text("x"))
            del app  # nothing but the harness refers to this session now
            gc.collect()
            await asyncio.sleep(0)
        gc.collect()

    run(scenario())
    gc.collect()
    assert _destroyed(caplog) == []


def test_sessions_are_kept_only_until_their_loop_closes() -> None:
    async def scenario() -> asyncio.AbstractEventLoop:
        await render_component(lambda: ft.Text("x"))
        loop = asyncio.get_running_loop()
        assert any(kept_loop is loop for kept_loop, _app in host_harness._LIVE)
        return loop

    loop = run(scenario())
    assert loop.is_closed()

    async def next_scenario() -> None:
        await render_component(lambda: ft.Text("y"))  # prunes the closed loop's sessions
        assert all(kept_loop is not loop for kept_loop, _app in host_harness._LIVE)

    run(next_scenario())
