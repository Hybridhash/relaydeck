"""Creation sheet sizing and recovery bounds through real Flet dispatch."""

from __future__ import annotations

import flet as ft
import pytest

from relaydeck.client.app import Root
from relaydeck.client.features.a11y.focus import iter_controls
from relaydeck.client.features.inventory.routing import RouteBridge
from relaydeck.client.pending_commands import MemoryCommandStore, PendingCommands
from tests.support.creation.pending_command_records import command
from tests.support.host_harness import render_component, run
from tests.support.scripted_sessions import make_scripted_sessions
from tests.support.ui.navigation import _state, _StubController, _StubTransport


@pytest.mark.parametrize("width", [390, 1280])
@pytest.mark.parametrize("kind", ["agent", "shell"])
def test_creation_sheet_has_one_natural_frame_and_measured_safe_bounds(
    width: int, kind: str
) -> None:
    async def scenario() -> None:
        journal = PendingCommands(MemoryCommandStore(), "https://gw", "acct", "dev")
        await journal.begin(command())
        controller = _StubController()
        bridge = RouteBridge(route="/", width=width, height=844)
        app = await render_component(
            Root,
            controller=controller,
            transport=_StubTransport(),
            host_id="home-mac",
            bridge=bridge,
            go=lambda route: None,
            session_factory=make_scripted_sessions().factory,
            account="acct",
            pending_commands=journal,
        )
        try:
            controller.push(_state())
            await app.settle()
            if kind == "shell":
                await app.set_props_and_fire(
                    "console-nav-terminals" if width == 390 else "console-rail-terminals",
                    {},
                    "click",
                )
                await app.set_props_and_fire("new-terminal-open", {}, "click")
            else:
                await app.set_props_and_fire("console-new-agent", {}, "click")
            workbench_id = app.find("workbench-shell")._i
            await app.set_props_and_fire(
                "terminal-operations-bar", {}, "size_change", {"w": width, "h": 106}
            )
            scrim, card, frame = (
                app.find(key) for key in ("sheet-scrim", "sheet-card", "sheet-frame")
            )
            assert (scrim.top, scrim.bottom, scrim.left, scrim.width, scrim.height) == (
                106,
                76 if width == 390 else 0,
                0,
                width,
                None,
            )
            assert card.width == min(520, width - 32)
            assert card.height is None and card.padding == 16
            assert isinstance(card.content, ft.Column) and card.content.tight
            assert card.content.scroll == ft.ScrollMode.AUTO
            await app.set_props_and_fire(
                "sheet-natural-body", {}, "size_change", {"w": card.width - 32, "h": 320}
            )
            assert app.find("sheet-card").height == 352
            await app.set_props_and_fire(
                "sheet-natural-body", {}, "size_change", {"w": card.width - 32, "h": 1000}
            )
            assert app.find("sheet-card").height == 844 - 106 - scrim.bottom - 32
            assert frame.border is None and frame.padding is None
            assert frame.content.tight and frame.content.spacing == 16
            assert frame.content.horizontal_alignment == ft.CrossAxisAlignment.STRETCH
            style = frame.theme.icon_button_theme.style
            assert style.bgcolor == ft.Colors.TRANSPARENT
            assert style.side.width == 0
            fields = [
                c for c in iter_controls([frame]) if isinstance(c, ft.Dropdown | ft.TextField)
            ]
            assert fields
            for field in fields:
                assert field.height is None
                assert field.content_padding.top == field.content_padding.bottom == 16
                if isinstance(field, ft.Dropdown):
                    assert field.error_text is None
                    assert field.expand and field.expanded_insets == 0
                    parent = next(
                        c
                        for c in iter_controls([frame])
                        if isinstance(c, ft.Row) and field in c.controls
                    )
                    assert (
                        not parent.wrap
                    )  # Horizontal flex gets finite width, never vertical scroll flex.
            actions = app.find("sheet-actions")
            assert not actions.wrap
            assert all(c.expand and c.height == 48 for c in actions.controls)
            # Expanded/collapsed recovery geometry updates without replacing the retained base.
            await app.set_props_and_fire(
                "terminal-operations-bar", {}, "size_change", {"w": width, "h": 240}
            )
            assert app.find("sheet-scrim").top == 240
            await app.set_props_and_fire(
                "terminal-operations-bar", {}, "size_change", {"w": width, "h": 0}
            )
            assert app.find("sheet-scrim").top == 0
            assert app.find("workbench-shell")._i == workbench_id
            # Validation expands naturally; no error is hidden behind a forced height.
            await app.set_props_and_fire("sheet-submit", {}, "click")
            assert app.find(f"{kind}-workspace").error_text
            assert app.find(f"{kind}-workspace").height is None
        finally:
            app.page.render(lambda: ft.Text("Closed"))
            await app.settle()

    run(scenario())
