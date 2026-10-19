"""A cold open keeps one layout from the connecting state through verification.

The connecting state, the taken-over state and the verified (input-ready) state reserve the same
height at every width tier, so verifying the writer causes no geometry change. The native side
re-sends its current grid at verification and on input enable; the open hold treats such an
unchanged grid as a re-send, not as layout activity, and releases at its quiet window.
"""

from __future__ import annotations

from typing import Any

import flet as ft
import pytest

from relaydeck.client.features.a11y.focus import iter_controls
from relaydeck.client.features.terminal.surfaces import agent_hooks
from relaydeck.client.features.terminal.surfaces.agent_hooks import use_open_hold
from relaydeck.client.features.terminal.surfaces.mount import mount_agent_screen
from tests.support.host_harness import render_component, run
from tests.support.terminal.sessions import _agent_session
from tests.support.ui.hook_clock import HookClock

pytestmark = pytest.mark.open_hold

KEY_BAR = "agent-mobile-keybar"


def _height_signature(app: Any) -> list[tuple[Any, ...]]:
    """Every control that can change the column's height: key, visibility, height, padding."""
    root = app.page.views[0]
    inside_key_bar: set[int] = set()
    for control in iter_controls([root], active_only=False):
        if getattr(control, "key", None) == KEY_BAR:
            inside_key_bar = {id(c) for c in iter_controls([control], active_only=False)}
    signature: list[tuple[Any, ...]] = []
    for control in iter_controls([root], active_only=False):
        if id(control) in inside_key_bar:
            continue  # The key bar lays out horizontally; its chips never change the height.
        key = getattr(control, "key", None)
        height = getattr(control, "height", None)
        padding = getattr(control, "padding", None)
        if key is None and height is None and padding is None:
            continue
        signature.append(
            (
                type(control).__name__,
                str(key),
                getattr(control, "visible", True),
                height,
                str(padding),
                getattr(control, "expand", None),
            )
        )
    return signature


@pytest.mark.parametrize(
    ("width", "phone", "tier"),
    [(390, True, "compact"), (768, True, "medium"), (1280, False, "expanded")],
    ids=["phone", "tablet", "wide"],
)
def test_connecting_taken_over_and_verified_states_reserve_the_same_height(
    width: int, phone: bool, tier: str
) -> None:
    async def scenario() -> None:
        session, _ops, _factory, _ = _agent_session()
        session.expect_control()
        session.open()
        assert session.control_pending
        app = await render_component(
            mount_agent_screen,
            session,
            phone=phone,
            width_tier=tier,
            touch=True,
            viewport_width=width,
            viewport_height=844,
        )
        connecting = _height_signature(app)
        await session.confirm_takeover()
        session.expect_control(False)
        await app.settle()
        taken_over = _height_signature(app)
        controller = session.controller
        assert controller is not None and session.keys_enabled
        controller.fence_ui_input(False)
        session._notify()
        await app.settle()
        verified = _height_signature(app)
        assert connecting == taken_over == verified

    run(scenario())


def _hold_probe(sent: list[tuple[float, tuple[int, int]]], clock: HookClock, out: dict[str, Any]):
    @ft.component
    def Probe() -> ft.Control:
        report, held = use_open_hold(
            (59, 54), lambda cols, rows: sent.append((clock.now, (cols, rows))), settled=True
        )
        out.update(report=report, held=held)
        return ft.Text("held" if held else "shown", key="probe")

    return Probe


def test_a_re_sent_unchanged_grid_releases_the_hold_at_the_quiet_window(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def scenario() -> None:
        clock = HookClock(monkeypatch, agent_hooks.__name__)
        sent: list[tuple[float, tuple[int, int]]] = []
        out: dict[str, Any] = {}
        app = await render_component(_hold_probe(sent, clock, out))
        start = clock.now
        out["report"](59, 54)  # the grid the native side re-sends at verification
        clock.advance_by(0.19)
        out["report"](59, 54)  # re-sent on input enable
        clock.advance_by(0.01)
        out["report"](59, 54)  # and again on the second input toggle
        clock.advance_to(start + agent_hooks.OPEN_SETTLE_S)
        await app.settle(rounds=1)
        assert sent == [(start + agent_hooks.OPEN_SETTLE_S, (59, 54))]
        assert out["held"] is False and app.find("probe").value == "shown"

    run(scenario())


def test_a_changed_grid_during_the_hold_still_restarts_the_quiet_window(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def scenario() -> None:
        clock = HookClock(monkeypatch, agent_hooks.__name__)
        sent: list[tuple[float, tuple[int, int]]] = []
        out: dict[str, Any] = {}
        app = await render_component(_hold_probe(sent, clock, out))
        start = clock.now
        out["report"](59, 54)
        clock.advance_by(0.19)
        out["report"](59, 23)  # the keyboard changed the grid: a real settle restarts quiet
        clock.advance_to(start + agent_hooks.OPEN_SETTLE_S)
        await app.settle(rounds=1)
        assert sent == [] and out["held"] is True
        clock.advance_to(start + 0.19 + agent_hooks.OPEN_SETTLE_S)
        await app.settle(rounds=1)
        assert sent == [(start + 0.19 + agent_hooks.OPEN_SETTLE_S, (59, 23))]

    run(scenario())


def test_a_keyboard_up_open_still_sends_exactly_one_settled_resize(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def scenario() -> None:
        clock = HookClock(monkeypatch, agent_hooks.__name__)
        sent: list[tuple[float, tuple[int, int]]] = []
        out: dict[str, Any] = {}
        app = await render_component(_hold_probe(sent, clock, out))
        start = clock.now
        for rows in (54, 40, 30, 23):  # the keyboard slides up over the first frames
            out["report"](59, rows)
            clock.advance_by(0.05)
        out["report"](59, 23)  # verification re-sends the settled grid
        clock.advance_to(start + 0.2 + agent_hooks.OPEN_SETTLE_S)
        await app.settle(rounds=1)
        assert [size for _, size in sent] == [(59, 23)]
        assert out["held"] is False

    run(scenario())
