"""Navigation and inventory.

Rendered on the real Flet 1.0.3 session via the host harness: no
Flutter, no browser. Destinations, lists, banners, filters, routing
and the creation sheets. Browser matrix (360/768/1280, back/forward,
zoom, screen reader) remains a manual release-gate item.
"""

from __future__ import annotations

import asyncio
from typing import Any

import flet as ft

from relaydeck.client.features.a11y.focus import iter_controls
from relaydeck.client.features.inventory.selectors import diagnostics
from relaydeck.domain.models import (
    Connection,
    ConnectionState,
    Host,
    Selection,
    Workspace,
)
from relaydeck.version import __version__
from tests.support.host_harness import run
from tests.support.ui.navigation import _render_root, _render_shell, _state


def test_destinations_lists_and_counts() -> None:
    async def scenario() -> None:
        state = _state()
        app = await _render_shell(state)
        assert any(
            isinstance(c, ft.Text) and c.value == "Console"
            for c in iter_controls([app.find("console-header")])
        )
        assert app.find("agents-count").value == "1 agents"
        assert any(
            isinstance(c, ft.Text) and c.value == "web"
            for c in iter_controls([app.find("agent-home-mac:term-1")])
        )
        await app.set_props_and_fire("console-rail-terminals", {}, "click")
        # The shell is presentational: destination flips come from the parent.
        app2 = await _render_shell(state, destination="terminals")
        header = [
            c.value
            for c in iter_controls([app2.find("terminals-header")])
            if isinstance(c, ft.Text)
        ]
        assert "Terminals" in header and "1 SHELL ON 1 HOST" in header
        assert app2.find("shell-home-mac:tab-1").tooltip == "shell-a"
        app3 = await _render_shell(state, destination="settings")
        host_card = app3.find("host-home-mac")
        assert any(
            isinstance(c, ft.Text) and c.value == "Home Mac" for c in iter_controls([host_card])
        )
        app4 = await _render_shell(state, destination="settings")
        assert app4.find("destination-title").value == "Settings"
        assert any(
            isinstance(c, ft.Text) and c.value == f"Version {__version__}"
            for c in iter_controls([app4.find("settings-version-row")])
        )

    run(scenario())


def test_wide_and_narrow_layouts() -> None:
    async def scenario() -> None:
        selected = _state(selection=Selection(host_id="home-mac", session_key="home-mac:term-1"))
        wide = await _render_shell(selected, is_wide=True)
        assert wide.find("destinations").width == 96
        assert wide.find("console-rail-console").on_click is not None
        assert wide.find("detail-title").value == "web"
        narrow_list = await _render_shell(_state(), is_wide=False)
        assert narrow_list.find("console-nav-console").on_click is not None
        assert narrow_list.find("agents-count").value == "1 agents"
        narrow_detail = await _render_shell(selected, is_wide=False)
        assert narrow_detail.find("detail-title").value == "web"
        assert narrow_detail.find("detail-back").tooltip == "Back"

    run(scenario())


def test_banner_matrix() -> None:
    async def scenario() -> None:
        revoked = await _render_shell(_state(revoked=True))
        assert "revoked" in revoked.find("banner-text").value.lower()
        assert revoked.find("banner-action").content == "Sign in"
        offline = await _render_shell(
            _state(connection=Connection(state=ConnectionState.HOST_OFFLINE, host_id="home-mac"))
        )
        assert "offline" in offline.find("banner-text").value.lower()
        recon = await _render_shell(
            _state(connection=Connection(state=ConnectionState.RECONNECTING, host_id="home-mac"))
        )
        # Retry notices have a two-second grace period; revocation/offline
        # actions above remain immediate.
        await asyncio.sleep(2.05)
        assert "Reconnecting" in recon.find("banner-text").value
        live = await _render_shell(_state())
        try:
            live.find("connection-banner")
            raise AssertionError("live state must not show a banner")
        except KeyError:
            pass

    run(scenario())


def test_missing_selection_shows_unavailable() -> None:
    async def scenario() -> None:
        state = _state(selection=Selection(host_id="home-mac", session_key="term-9"))
        app = await _render_shell(state, is_wide=True)
        assert "unavailable" in app.find("detail-missing").value.lower()
        empty = await _render_shell(_state(), is_wide=True)
        assert "Select a session" in empty.find("detail-empty").value

    run(scenario())


def test_filter_preserved_across_state_update() -> None:
    async def scenario() -> None:
        app, controller, _, _, _ = await _render_root(_state())
        await app.set_props_and_fire("inventory-query", {"value": "web"}, "change", "web")
        assert app.find("inventory-query").value == "web"
        controller.push(_state())
        await app.settle()
        assert app.find("inventory-query").value == "web"
        assert any(
            isinstance(c, ft.Text) and c.value == "web"
            for c in iter_controls([app.find("agent-home-mac:term-1")])
        )

    run(scenario())


def test_selection_pushes_route_and_back_restores() -> None:
    async def scenario() -> None:
        app, _, _, bridge, pushed = await _render_root(_state())
        await app.set_props_and_fire("agent-home-mac:term-1", {}, "click")
        await app.settle()
        assert pushed == ["/agents/home-mac:term-1"]
        assert app.find("agent-detail-title").value == "web"
        bridge.set_route("/agents")
        await app.settle()
        assert "Select a session" in app.find("detail-empty").value

    run(scenario())


def test_start_agent_sheet_validates_and_submits() -> None:
    async def scenario() -> None:
        app, _, transport, _, _ = await _render_root(_state())
        await app.set_props_and_fire("console-new-agent", {}, "click")
        assert app.find("start-agent-sheet") is not None
        await app.set_props_and_fire("sheet-submit", {}, "click")
        assert app.find("agent-workspace").error_text == "Choose a workspace."
        assert app.find("agent-name").error is None
        assert transport.submitted == []
        await app.set_props_and_fire("agent-workspace", {"value": "w1"}, "select", "w1")
        await app.set_props_and_fire("agent-name", {"value": "web"}, "change", "web")
        await app.set_props_and_fire("sheet-submit", {}, "click")
        assert len(transport.submitted) == 1
        body = transport.submitted[0]
        assert body["kind"] == "agent.start"
        assert body["target"] == {"workspace_id": "w1"}
        assert body["payload"] == {"kind": "tool-a", "name": "web"}
        assert body["host_epoch"] == "epoch-7"
        try:
            app.find("start-agent-sheet")
            raise AssertionError("sheet must close after accept")
        except KeyError:
            pass

    run(scenario())


def test_host_change_clears_workspace() -> None:
    async def scenario() -> None:
        state = _state()
        other = Host(id="work-mac", label="Work Mac", epoch="epoch-3", reachable=True)
        state.inventory.hosts["work-mac"] = other
        state.inventory.workspaces["w9"] = Workspace(id="w9", host_id="work-mac", label="job")
        app, _, _, _, _ = await _render_root(state)
        await app.set_props_and_fire("console-new-agent", {}, "click")
        await app.set_props_and_fire("agent-workspace", {"value": "w1"}, "select", "w1")
        assert app.find("agent-workspace").value == "w1"
        await app.set_props_and_fire("agent-host", {"value": "work-mac"}, "select", "work-mac")
        assert app.find("agent-workspace").value in ("", None)
        await app.set_props_and_fire("sheet-submit", {}, "click")
        assert app.find("agent-workspace").error_text == "Choose a workspace."

    run(scenario())


def test_cancel_drops_draft() -> None:
    async def scenario() -> None:
        app, _, _, _, _ = await _render_root(_state())
        await app.set_props_and_fire("console-rail-terminals", {}, "click")
        await app.set_props_and_fire("new-terminal-open", {}, "click")
        await app.set_props_and_fire("shell-label", {"value": "scratch"}, "change", "scratch")
        await app.set_props_and_fire("sheet-cancel", {}, "click")
        try:
            app.find("new-terminal-sheet")
            raise AssertionError("sheet must close on cancel")
        except KeyError:
            pass
        await app.set_props_and_fire("new-terminal-open", {}, "click")
        assert app.find("shell-label").value in ("", None)

    run(scenario())


def test_diagnostics_rendered_with_redaction() -> None:
    async def scenario() -> None:
        state = _state()
        model = diagnostics(
            state,
            now_ms=61_000,
            gateway_reachable=True,
            socket_status="live",
            app_version="0.1.0",
            correlation_id="corr-abcdef1234",
        )
        app = await _render_shell(state, destination="settings", diagnostics=model)
        assert "live" in app.find("diagnostics-Connection").value
        assert "cccc" in app.find("diagnostics-Cursor").value
        assert "c" * 32 not in app.find("diagnostics-Cursor").value
        assert app.find("diagnostics-Correlation").value.endswith("1234")
        assert "abcdef" not in app.find("diagnostics-Correlation").value

    run(scenario())


def test_open_sheet_keeps_rail_bounded_behind_modal_scrim() -> None:
    """Opening a creation sheet must not destroy the sidebar: the
    overlay Stack expands (the page view sizes children unbounded, so
    a bare Stack re-triggers the rail's "height is unbounded" red
    screen), and the sheet renders as a centered modal card over a
    full-viewport scrim. Found by a live browser run."""

    async def scenario() -> None:
        app, _, _, _, _ = await _render_root(_state())
        await app.set_props_and_fire("console-new-agent", {}, "click")
        assert app.find("start-agent-sheet") is not None
        rail = app.find("destinations")
        assert isinstance(rail, ft.Container) and rail.width == 96
        assert app.find("console-rail-console").on_click is not None
        roots: list[Any] = [app.page.views[0].controls]
        seen_stack = False
        for control in iter_controls(roots):
            if isinstance(control, ft.Stack):
                assert control.expand, "overlay Stack must expand (bounds the rail)"
                seen_stack = True
        assert seen_stack, "open sheet must render an overlay Stack"
        scrim = app.find("sheet-scrim")
        assert (scrim.width, scrim.height, scrim.top, scrim.bottom) == (1280, None, 0, 0)
        assert app.find("sheet-card") is not None

    run(scenario())


def test_wide_shell_bounds_navigation_rail_height() -> None:
    """The NavigationRail sits in an expanded Row inside an expanded
    SafeArea: the page view sizes children unbounded, and inside an
    unbounded parent the rail fails to render ("height is unbounded").
    Found by a live browser run; pinned structurally (headless tests do
    no layout)."""

    async def scenario() -> None:
        app = await _render_shell(_state())
        rail = app.find("destinations")
        assert isinstance(rail, ft.Container) and rail.width == 96
        assert app.find("console-rail-console").on_click is not None
        roots: list[Any] = [app.page.views[0].controls]
        seen_row = False
        seen_area = False
        for control in iter_controls(roots):
            if control is app.find("inventory-workbench"):
                assert isinstance(control, ft.Row)
                assert app.find("rail-slot").content is rail
                assert control.expand, "rail parent Row must expand (bounds height)"
                assert control.vertical_alignment == ft.CrossAxisAlignment.STRETCH
                seen_row = True
            if isinstance(control, ft.SafeArea):
                assert control.expand, "root SafeArea must expand (bounds the Row)"
                seen_area = True
        assert seen_row, "rail has no bounded workbench Row"
        assert seen_area, "shell has no SafeArea"

    run(scenario())
