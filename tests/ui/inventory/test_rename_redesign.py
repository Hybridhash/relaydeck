"""Console rename: gestures, sheet, optimistic name, Undo, failure and Retry."""

from __future__ import annotations

import asyncio
import logging
from dataclasses import replace
from typing import Any

import flet as ft
import pytest

from relaydeck.client.components.design_system import CountTile
from relaydeck.client.components.design_system.primitives import COUNT_TILE_HEIGHT
from relaydeck.client.features.a11y.focus import iter_controls
from relaydeck.client.features.inventory.components import AppShell
from relaydeck.client.features.inventory.components.lists import AgentsList
from relaydeck.client.features.inventory.selectors import AgentRow
from relaydeck.client.features.sync.transport import TransportError
from relaydeck.client.features.terminal.diagnostics import diagnostics_logger
from relaydeck.domain.models import AgentStatus
from tests.support.host_harness import HostApp, button_label, render_component, run
from tests.support.ui.navigation import _state

KEY = "home-mac:term-1"


class Transport:
    def __init__(self) -> None:
        self.submitted: list[dict[str, Any]] = []
        self.status = "succeeded"
        self.release = asyncio.Event()
        self.release.set()
        self.lose_response = False

    def record(self, command: dict[str, Any], status: str) -> dict[str, Any]:
        keys = ("protocol", "command_id", "host_id", "kind", "target")
        body = {**{key: command[key] for key in keys}, "status": status, "updated_ms": 1}
        return body if status != "failed" else {**body, "error_code": "agent_name_taken"}

    async def submit_command(self, command: dict[str, Any]) -> dict[str, Any]:
        self.submitted.append(command)
        await self.release.wait()
        if self.lose_response:
            raise TransportError("unavailable", "response lost", retryable=True)
        return self.record(command, "accepted")

    async def command_status(self, host: str, command_id: str) -> dict[str, Any]:
        return self.record(self.submitted[-1], self.status)


def _texts(app: HostApp) -> list[str]:
    nodes = iter_controls([app.page.views[0].controls])
    return [str(c.value) for c in nodes if isinstance(c, ft.Text) and c.value]


async def _render(
    width: int,
    transport: Transport,
    *,
    task: str = "Review release notes",
    name: str = "web",
    dark: bool = True,
    selected: list[str] | None = None,
) -> Any:
    selected = [] if selected is None else selected

    @ft.component
    def Parent() -> Any:
        initial = _state()
        agent = initial.inventory.agents[KEY]
        inventory = replace(initial.inventory, agents={KEY: replace(agent, title=task, name=name)})
        state = replace(initial, inventory=replace(inventory))
        return AppShell(
            state=state,
            is_wide=width >= 1100,
            viewport_width=width,
            destination="agents",
            command_transport=transport,
            foreground=True,
            dark=dark,
            on_destination=lambda value: None,
            on_select_agent=selected.append,
            on_select_shell=lambda value: None,
            on_select_host=lambda value: None,
            on_banner_action=lambda: None,
            on_open_terminal=lambda: None,
            on_start_agent=lambda: None,
            on_new_terminal=lambda: None,
        )

    return await render_component(Parent)


async def _open_sheet(app: HostApp) -> None:
    await app.set_props_and_fire(f"agent-gesture-{KEY}", {}, "long_press_start")
    await app.set_props_and_fire(f"agent-rename-{KEY}", {}, "click")


async def _type(app: HostApp, text: str) -> None:
    await app.set_props_and_fire("agent-rename-name", {"value": text}, "change", text)


@pytest.mark.parametrize("width", [390, 1280])
def test_long_press_menu_lists_rename_first_then_open_and_opens_the_sheet(width: int) -> None:
    async def scenario() -> None:
        app = await _render(width, Transport())
        assert not any(
            c.icon == ft.Icons.MORE_VERT
            for c in iter_controls([app.page.views[0].controls])
            if hasattr(c, "icon")
        )
        await app.set_props_and_fire(f"agent-gesture-{KEY}", {}, "long_press_start")
        menu = app.find(f"agent-menu-{KEY}")
        rows = menu.content.content.controls
        assert [row.key for row in rows] == [
            f"agent-rename-{KEY}",
            f"agent-pin-{KEY}",
            f"agent-open-{KEY}",
        ]
        assert button_label(rows[0].content.controls[0]) == "Rename"
        await app.set_props_and_fire(f"agent-rename-{KEY}", {}, "click")
        sheet = app.find("agent-rename-dialog")
        assert sheet.open and isinstance(sheet, ft.BottomSheet if width < 600 else ft.AlertDialog)

    run(scenario())


@pytest.mark.parametrize("width", [390, 1280])
def test_right_click_and_swipe_and_f2_open_the_same_sheet(width: int) -> None:
    async def scenario() -> None:
        for gesture in (
            (f"agent-gesture-{KEY}", "secondary_tap"),
            (f"agent-swipe-{KEY}", "confirm_dismiss"),
            (f"agent-keys-{KEY}", "key_down"),
        ):
            app = await _render(width, Transport())
            if gesture[1] == "key_down":
                await app.session.dispatch_event(app.find(gesture[0])._i, "key_down", {"key": "F2"})
                await app.settle()
            else:
                await app.set_props_and_fire(gesture[0], {}, gesture[1])
            if gesture[1] == "secondary_tap":
                await app.set_props_and_fire(f"agent-rename-{KEY}", {}, "click")
            assert app.find("agent-rename-dialog").open, gesture

    run(scenario())


@pytest.mark.parametrize("width", [390, 1280])
def test_sheet_preselects_the_name_and_a_suggestion_fills_the_field(width: int) -> None:
    async def scenario() -> None:
        app = await _render(width, Transport())
        await _open_sheet(app)
        field = app.find("agent-rename-name")
        assert field.value == "web" and field.autofocus
        assert (field.selection.base_offset, field.selection.extent_offset) == (0, 3)
        assert app.find("agent-rename-save").disabled  # unchanged
        assert "AGENT NAME" in _texts(app) and "Shown on cards, terminals and Usage" in _texts(app)
        assert app.find("agent-rename-count").value == "3 / 32"
        chip = app.find("agent-rename-suggestion-0")
        name = button_label(chip)
        await app.set_props_and_fire("agent-rename-suggestion-0", {}, "click")
        assert app.find("agent-rename-name").value == name
        assert not app.find("agent-rename-save").disabled
        assert not app.find("agent-rename-name").selection

    run(scenario())


@pytest.mark.parametrize("width", [390, 1280])
def test_save_is_disabled_for_empty_long_and_duplicate_names(width: int) -> None:
    async def scenario() -> None:
        app = await _render(width, Transport())
        await _open_sheet(app)
        await _type(app, "")
        assert app.find("agent-rename-save").disabled
        assert app.find("agent-rename-name").error == "Name can't be empty"
        await _type(app, "x" * 40)
        assert app.find("agent-rename-name").value == "x" * 32
        assert app.find("agent-rename-count").value == "32 / 32"
        await _type(app, "Release Notes")
        assert not app.find("agent-rename-save").disabled

    run(scenario())


def _card_title(app: HostApp) -> str:
    card = app.find(f"agent-{KEY}")
    return str(next(c.value for c in iter_controls([card]) if isinstance(c, ft.Text)))


@pytest.mark.parametrize("width", [390, 1280])
def test_save_updates_the_card_at_once_then_undo_restores_it_without_a_second_toast(
    width: int,
) -> None:
    async def scenario() -> None:
        transport = Transport()
        app = await _render(width, transport)
        await _open_sheet(app)
        await _type(app, "Release Notes")
        await app.set_props_and_fire("agent-rename-save", {}, "click")
        assert transport.submitted[0]["payload"] == {"name": "Release Notes"}
        assert _card_title(app) == "Release Notes"
        toast = app.find(f"agent-rename-toast-{KEY}")
        assert toast.open and toast.duration == 5000
        assert toast.action.label == "Undo"
        card = app.find(f"agent-{KEY}")
        assert card.content.border.top.color != card.content.border.left.color
        toast.action.on_click(None)
        await app.settle()
        assert transport.submitted[1]["payload"] == {"name": "web"}
        assert _card_title(app) == "web"
        with pytest.raises(KeyError):
            app.find(f"agent-rename-toast-{KEY}")

    run(scenario())


@pytest.mark.parametrize("width", [390, 1280])
def test_a_refused_rename_reverts_and_offers_retry(width: int) -> None:
    async def scenario() -> None:
        transport = Transport()
        transport.status = "failed"
        app = await _render(width, transport)
        await _open_sheet(app)
        await _type(app, "Release Notes")
        await app.set_props_and_fire("agent-rename-save", {}, "click")
        assert _card_title(app) == "web"
        toast = app.find(f"agent-rename-toast-{KEY}")
        assert toast.action.label == "Retry"
        assert app.find("agent-rename-name").error == "Another agent already uses this name."
        transport.status = "succeeded"
        toast.action.on_click(None)
        await app.settle()
        assert not app.find("agent-rename-dialog").open  # the shown sheet closes first
        await _deliver(app, app.find("agent-rename-dialog")._i)
        assert len(transport.submitted) == 2
        assert transport.submitted[1]["payload"] == {"name": "Release Notes"}
        assert _card_title(app) == "Release Notes"

    run(scenario())


@pytest.mark.parametrize("width", [390, 1280])
def test_unread_shows_as_text_in_the_meta_row_and_clears_when_read(width: int) -> None:
    async def scenario() -> None:
        row = AgentRow(
            key=KEY,
            title="web",
            subtitle="tool-a · web · 2 min ago",
            status=AgentStatus.WORKING,
            focused=False,
            unread=True,
            host_id="home-mac",
            workspace_id="w1",
            agent="tool-a",
            subject="Review release notes",
        )

        @ft.component
        def Host() -> Any:
            return AgentsList(
                rows=(row,),
                selected="",
                on_select=lambda key: None,
                on_start_agent=lambda: None,
                phone=width < 600,
            )

        app = await render_component(Host)
        assert _texts(app)[-1] == "Unread"  # bottom-right meta position
        with pytest.raises(KeyError):
            app.find(f"agent-unread-{KEY}")  # no dot in the title row
        read = replace(row, unread=False)

        @ft.component
        def Quiet() -> Any:
            return AgentsList(
                rows=(read,),
                selected="",
                on_select=lambda key: None,
                on_start_agent=lambda: None,
            )

        quiet = await render_component(Quiet)
        assert "Unread" not in _texts(quiet)

    run(scenario())


def test_counter_tiles_are_compact_side_by_side_strips() -> None:
    tile = CountTile("working", 4)
    assert tile.height == COUNT_TILE_HEIGHT == 52
    assert isinstance(tile.content, ft.Row)
    number, label = tile.content.controls
    assert number.value == "4" and label.value == "WORKING"


def test_undo_toast_is_not_started_for_a_cancelled_sheet() -> None:
    async def scenario() -> None:
        transport = Transport()
        app = await _render(390, transport)
        await _open_sheet(app)
        await _type(app, "Release Notes")
        await app.set_props_and_fire("agent-rename-cancel", {}, "click")
        await asyncio.sleep(0)
        assert not transport.submitted and _card_title(app) == "web"
        with pytest.raises(KeyError):
            app.find(f"agent-rename-toast-{KEY}")

    run(scenario())


def test_the_highlight_ring_is_static_and_clears_after_a_moment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from relaydeck.client.features.inventory import rename_flow

    monkeypatch.setattr(rename_flow, "HIGHLIGHT_SECONDS", 0.6)

    async def scenario() -> None:
        app = await _render(390, Transport())
        await _open_sheet(app)
        await _type(app, "Release Notes")
        await app.set_props_and_fire("agent-rename-save", {}, "click")
        assert app.find(f"agent-{KEY}").content.shadow
        await asyncio.sleep(1.0)
        await app.settle()
        assert not app.find(f"agent-{KEY}").content.shadow

    run(scenario())


def _hidden_pending(app: HostApp) -> bool:
    return not app.find("agent-rename-dialog").open


@pytest.mark.parametrize("width", [390, 1280])
def test_the_native_close_acknowledgement_of_a_hide_does_not_end_the_run(width: int) -> None:
    async def scenario() -> None:
        transport = Transport()
        transport.release.clear()
        app = await _render(width, transport)
        await _open_sheet(app)
        await _type(app, "Release Notes")
        await app.set_props_and_fire("agent-rename-save", {}, "click")
        assert _hidden_pending(app) and _card_title(app) == "Release Notes"
        await app.set_props_and_fire("agent-rename-dialog", {"open": False}, "dismiss")
        assert app.find("agent-rename-dialog")
        transport.release.set()
        await app.settle()
        assert len(transport.submitted) == 1
        toast = app.find(f"agent-rename-toast-{KEY}")
        assert toast.action.label == "Undo" and _card_title(app) == "Release Notes"
        with pytest.raises(KeyError):
            app.find("agent-rename-dialog")

    run(scenario())


async def _fail_after_hide(width: int, how: str) -> tuple[Any, Transport, int]:
    """Save, hide, then fail. Returns the app, the transport and the hidden presentation's id."""
    transport = Transport()
    transport.release.clear()
    if how == "refused":
        transport.status = "failed"
    else:
        transport.lose_response = True
    app = await _render(width, transport)
    await _open_sheet(app)
    await _type(app, "Release Notes")
    await app.set_props_and_fire("agent-rename-save", {}, "click")
    hidden = app.find("agent-rename-dialog")
    assert not hidden.open
    transport.release.set()
    await app.settle()
    return app, transport, hidden._i


async def _deliver(app: Any, control_id: int, event: str = "dismiss") -> None:
    await app.session.dispatch_event(control_id, event, "")
    await app.settle()


@pytest.mark.parametrize("delay", [0.0, 1.5])
@pytest.mark.parametrize("how", ["refused", "unknown"])
@pytest.mark.parametrize("width", [390, 1280])
def test_a_close_report_of_an_earlier_hide_never_ends_the_run_however_late(
    width: int, how: str, delay: float
) -> None:
    async def scenario() -> None:
        app, transport, hidden_id = await _fail_after_hide(width, how)
        current = app.find("agent-rename-dialog")
        assert current.open and current._i != hidden_id  # a new presentation was mounted
        assert app.find("agent-rename-name").error
        await asyncio.sleep(delay)
        # The earlier hide's real close report (open=False, then dismiss), delivered late.
        await _deliver(app, hidden_id, "dismiss")
        assert app.find("agent-rename-dialog").open and app.find("agent-rename-dialog")._i == (
            current._i
        )
        if how == "unknown":
            assert app.find("agent-rename-check").visible
        assert len(transport.submitted) == 1  # never resubmitted
        assert _card_title(app) == "web"

    run(scenario())


@pytest.mark.parametrize("width", [390, 1280])
def test_the_hide_report_before_any_reopen_is_not_user_intent(width: int) -> None:
    async def scenario() -> None:
        transport = Transport()
        transport.release.clear()
        app = await _render(width, transport)
        await _open_sheet(app)
        await _type(app, "Release Notes")
        await app.set_props_and_fire("agent-rename-save", {}, "click")
        await app.set_props_and_fire("agent-rename-dialog", {"open": False}, "dismiss")
        assert app.find("agent-rename-dialog")  # still mounted, still observing
        transport.release.set()
        await app.settle()
        assert app.find(f"agent-rename-toast-{KEY}").action.label == "Undo"

    run(scenario())


@pytest.mark.parametrize("width", [390, 1280])
def test_a_real_dismissal_of_the_current_presentation_closes_it(width: int) -> None:
    async def scenario() -> None:
        app, _, _ = await _fail_after_hide(width, "refused")
        await app.set_props_and_fire("agent-rename-dialog", {"open": False}, "dismiss")
        with pytest.raises(KeyError):
            app.find("agent-rename-dialog")

    run(scenario())


@pytest.mark.parametrize("width", [390, 1280])
def test_explicit_cancel_closes_the_reopened_sheet(width: int) -> None:
    async def scenario() -> None:
        app, _, hidden_id = await _fail_after_hide(width, "unknown")
        await _deliver(app, hidden_id)
        assert app.find("agent-rename-dialog").open
        await app.set_props_and_fire("agent-rename-cancel", {}, "click")
        assert not app.find("agent-rename-dialog").open  # closes natively before it unmounts
        await app.session.dispatch_event(app.find("agent-rename-dialog")._i, "dismiss", "")
        await app.settle()
        with pytest.raises(KeyError):
            app.find("agent-rename-dialog")

    run(scenario())


def _spy_invocations(app: HostApp) -> list[tuple[str, Any, Any]]:
    calls: list[tuple[str, Any, Any]] = []

    async def spy(control_id: int, name: str, args: Any, timeout: Any = None) -> Any:
        calls.append((name, args, timeout))

    app.session.invoke_method = spy  # type: ignore[method-assign]
    return calls


@pytest.mark.parametrize("width", [390, 1280])
def test_a_swipe_resets_the_card_with_a_bounded_invocation_before_the_sheet_opens(
    width: int,
) -> None:
    async def scenario() -> None:
        app = await _render(width, Transport())
        calls = _spy_invocations(app)
        await app.set_props_and_fire(f"agent-swipe-{KEY}", {}, "confirm_dismiss")
        assert calls == [("confirm_dismiss", {"dismiss": False}, 2)]
        assert app.find("agent-rename-dialog").open

    run(scenario())


def test_a_swipe_without_an_answer_leaves_no_pending_invocation_and_still_opens() -> None:
    async def scenario() -> None:
        app = await _render(390, Transport())
        await app.set_props_and_fire(f"agent-swipe-{KEY}", {}, "confirm_dismiss")
        assert not app.session._Session__method_calls  # type: ignore[attr-defined]
        assert not app.session._Session__method_call_results  # type: ignore[attr-defined]
        assert app.find("agent-rename-dialog").open

    run(scenario())


def test_a_swipe_on_a_control_that_already_left_the_tree_still_opens_rename() -> None:
    from types import SimpleNamespace

    from relaydeck.client.features.inventory.agent_gestures import AgentGestures

    async def scenario() -> None:
        opened: list[bool] = []

        class Gone:
            async def _invoke_method(self, *args: Any, **kwargs: Any) -> None:
                raise RuntimeError("Control must be added to the page first")

        swipe = AgentGestures(
            "k",
            ft.Container(),
            "label",
            True,
            lambda: None,
            lambda: opened.append(True),
            lambda: None,
        )
        await swipe.on_confirm_dismiss(SimpleNamespace(control=Gone()))
        assert opened == [True]

    run(scenario())


def test_an_override_does_not_revive_when_another_device_renames_back() -> None:
    from relaydeck.client.features.inventory.rename_flow import use_rename_flow
    from relaydeck.client.features.inventory.rename_wiring import shown_title

    async def scenario() -> None:
        handles: dict[str, Any] = {}
        base = AgentRow(
            key=KEY, title="web", subtitle="", status=AgentStatus.WORKING, focused=False
        )

        @ft.component
        def Parent() -> Any:
            title, set_title = ft.use_state("web")
            row = replace(base, title=title)
            flow = use_rename_flow((row,))
            handles.update(flow=flow, set_title=set_title)
            return ft.Text(shown_title(row, flow.overrides), key="shown")

        app = await render_component(Parent)
        handles["flow"].open_rename(KEY, "web")
        await app.settle()
        flow = handles["flow"]
        flow._actions["pending"](flow.run)("Release Notes")
        flow._actions["result"](flow.run)("Release Notes", True)
        await app.settle()
        assert app.find("shown").value == "Release Notes"
        for title in ("Release Notes", "web"):
            handles["set_title"](title)
            await app.settle()
        assert app.find("shown").value == "web"

    run(scenario())


def test_a_stale_attempt_cannot_clear_a_newer_override() -> None:
    from relaydeck.client.features.inventory.rename_flow import use_rename_flow

    async def scenario() -> None:
        handles: dict[str, Any] = {}
        base = AgentRow(
            key=KEY, title="web", subtitle="", status=AgentStatus.WORKING, focused=False
        )

        @ft.component
        def Parent() -> Any:
            flow = use_rename_flow((base,))
            handles["flow"] = flow
            return ft.Text(",".join(flow.overrides), key="keys")

        app = await render_component(Parent)
        flow = handles["flow"]
        flow.open_rename(KEY, "web")
        await app.settle()
        old = handles["flow"].run
        handles["flow"].open_rename(KEY, "web")
        await app.settle()
        new = handles["flow"].run
        handles["flow"]._actions["pending"](new)("Newer")
        handles["flow"]._actions["result"](old)("Older", False)
        await app.settle()
        assert KEY in handles["flow"].overrides and handles["flow"].toast is None
        handles["flow"]._actions["abandon"](old)("Older")
        assert KEY in handles["flow"].overrides

    run(scenario())


@pytest.mark.parametrize(("name", "units"), [("🚀 Plan", 7), ("👨\u200d👩\u200d👧", 8), ("web", 3)])
def test_preselection_covers_the_whole_name_in_utf16_units(name: str, units: int) -> None:
    async def scenario() -> None:
        app = await _render(390, Transport(), name=name)
        await _open_sheet(app)
        selection = app.find("agent-rename-name").selection
        assert (selection.base_offset, selection.extent_offset) == (0, units)

    run(scenario())


@pytest.mark.parametrize("width", [390, 1280])
def test_light_theme_cards_carry_the_redesigned_behaviour(width: int) -> None:
    async def scenario() -> None:
        transport = Transport()
        app = await _render(width, transport, dark=False)
        await _open_sheet(app)
        await _type(app, "Release Notes")
        await app.set_props_and_fire("agent-rename-save", {}, "click")
        tile = app.find(f"agent-{KEY}")
        title = (
            tile.title.content.value
            if isinstance(tile.title, ft.GestureDetector)
            else tile.title.value
        )
        assert title == "Release Notes"
        assert tile.shape.side is not None and tile.shape.side.width == 2
        if width >= 600:
            assert isinstance(tile.title, ft.GestureDetector)

    run(scenario())


def test_light_theme_unread_is_text() -> None:
    from relaydeck.client.components.session_rows import agent_tile

    row = AgentRow(
        key=KEY, title="web", subtitle="", status=AgentStatus.WORKING, focused=False, unread=True
    )

    async def scenario() -> None:
        @ft.component
        def Both() -> Any:
            return ft.Column(
                [
                    agent_tile(row, False, lambda key: None, dark=False),
                    agent_tile(replace(row, unread=False, key="q"), False, lambda k: None, False),
                ]
            )

        app = await render_component(Both)
        assert app.find(f"unread-{KEY}").value == "Unread" and app.find(f"unread-{KEY}").visible
        assert not app.find("unread-q").visible
        with pytest.raises(KeyError):
            app.find(f"agent-unread-{KEY}")

    run(scenario())


@pytest.mark.parametrize("width", [390, 1280])
def test_clear_and_suggestion_hit_areas_are_at_least_44(width: int) -> None:
    async def scenario() -> None:
        app = await _render(width, Transport())
        await _open_sheet(app)
        clear = app.find("agent-rename-name").suffix
        assert clear.width >= 44 and clear.height >= 44
        chip = app.find("agent-rename-suggestion-0")
        assert isinstance(chip, ft.TextButton) and not chip.disabled
        assert chip.height >= 44 and chip.content.height == 36
        assert chip.style.side[ft.ControlState.FOCUSED].width == 2  # visible keyboard focus
        picked = button_label(chip)
        await app.set_props_and_fire("agent-rename-suggestion-0", {}, "click")
        assert app.find("agent-rename-name").value == picked

    run(scenario())


def _present(app: HostApp, key: str) -> bool:
    try:
        app.find(key)
    except KeyError:
        return False
    return True


async def _closes_then_unmounts(app: HostApp, key: str) -> None:
    """The control is patched open=False first and unmounts only on the client's close report."""
    control = app.find(key)
    assert not control.open
    await app.session.dispatch_event(control._i, "dismiss", "")
    await app.settle()
    assert not _present(app, key)


@pytest.mark.parametrize("width", [390, 1280])
def test_cancel_closes_the_open_sheet_first_and_sends_no_command(width: int) -> None:
    async def scenario() -> None:
        transport = Transport()
        app = await _render(width, transport)
        await _open_sheet(app)
        assert app.find("agent-rename-dialog").open
        await app.set_props_and_fire("agent-rename-cancel", {}, "click")
        assert _present(app, "agent-rename-dialog")
        await _closes_then_unmounts(app, "agent-rename-dialog")
        assert transport.submitted == [] and _card_title(app) == "web"

    run(scenario())


def test_cancel_unmounts_after_a_short_fallback_when_no_report_comes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from relaydeck.client.features.inventory import rename_presentation

    monkeypatch.setattr(rename_presentation, "CLOSE_FALLBACK_S", 0.4)

    async def scenario() -> None:
        app = await _render(390, Transport())
        await _open_sheet(app)
        await app.set_props_and_fire("agent-rename-cancel", {}, "click")
        assert not app.find("agent-rename-dialog").open
        await asyncio.sleep(0.8)
        await app.settle()
        assert not _present(app, "agent-rename-dialog")

    run(scenario())


@pytest.mark.parametrize("width", [390, 1280])
def test_a_swipe_down_dismissal_of_the_open_sheet_closes_it(width: int) -> None:
    async def scenario() -> None:
        app = await _render(width, Transport())
        await _open_sheet(app)
        await app.set_props_and_fire("agent-rename-dialog", {"open": False}, "dismiss")
        assert not _present(app, "agent-rename-dialog")

    run(scenario())


@pytest.mark.parametrize("width", [390, 1280])
def test_the_menu_closes_first_for_rename_and_for_open_terminal(width: int) -> None:
    async def scenario() -> None:
        selected: list[str] = []
        app = await _render(width, Transport(), selected=selected)
        menu_key = f"agent-menu-{KEY}"
        await app.set_props_and_fire(f"agent-gesture-{KEY}", {}, "long_press_start")
        assert app.find(menu_key).open
        await app.set_props_and_fire(f"agent-rename-{KEY}", {}, "click")
        await _closes_then_unmounts(app, menu_key)
        assert app.find("agent-rename-dialog").open and not selected
        await app.set_props_and_fire("agent-rename-cancel", {}, "click")
        await _closes_then_unmounts(app, "agent-rename-dialog")
        await app.set_props_and_fire(f"agent-gesture-{KEY}", {}, "long_press_start")
        await app.set_props_and_fire(f"agent-open-{KEY}", {}, "click")
        assert selected == []  # navigation waits for the close report
        await _closes_then_unmounts(app, menu_key)
        assert selected == [KEY]
        assert not _present(app, "agent-rename-dialog")

    run(scenario())


@pytest.mark.parametrize("width", [390, 1280])
def test_a_dismissed_menu_unmounts_on_its_own_report(width: int) -> None:
    async def scenario() -> None:
        app = await _render(width, Transport())
        await app.set_props_and_fire(f"agent-gesture-{KEY}", {}, "long_press_start")
        await app.set_props_and_fire(f"agent-menu-{KEY}", {"open": False}, "dismiss")
        assert not _present(app, f"agent-menu-{KEY}")

    run(scenario())


@pytest.mark.parametrize("width", [390, 1280])
def test_a_toast_is_closed_before_the_next_rename_replaces_it(width: int) -> None:
    async def scenario() -> None:
        app = await _render(width, Transport())
        await _open_sheet(app)
        await _type(app, "Release Notes")
        await app.set_props_and_fire("agent-rename-save", {}, "click")
        toast_key = f"agent-rename-toast-{KEY}"
        assert app.find(toast_key).open
        await _open_sheet(app)
        assert _present(app, toast_key) and not app.find(toast_key).open

    run(scenario())


@pytest.mark.parametrize("width", [390, 1280])
def test_the_field_spans_the_sheet_and_the_nav_buttons_are_plain_text(width: int) -> None:
    async def scenario() -> None:
        app = await _render(width, Transport())
        await _open_sheet(app)
        sheet = app.find("agent-rename-dialog")
        body = sheet.content.content if width < 600 else sheet.content.content
        assert body.horizontal_alignment == ft.CrossAxisAlignment.STRETCH
        clear = app.find("agent-rename-name").suffix
        assert isinstance(clear, ft.Container) and clear.border is None and clear.bgcolor is None
        for key in ("agent-rename-cancel", "agent-rename-save"):
            style = app.find(key).style
            assert style.side.width == 0 and style.bgcolor == ft.Colors.TRANSPARENT

    run(scenario())


@pytest.mark.parametrize("width", [390, 1280])
@pytest.mark.parametrize(
    ("target", "event", "source"),
    [
        (f"agent-{KEY}", "long_press", "card"),
        (f"agent-gesture-{KEY}", "long_press_start", "long_press"),
        (f"agent-gesture-{KEY}", "secondary_tap", "secondary"),
    ],
)
def test_a_hold_opens_the_menu_from_each_source_and_logs_content_free_lines(
    width: int, target: str, event: str, source: str, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.INFO)

    async def scenario() -> None:
        selected: list[str] = []
        app = await _render(width, Transport(), selected=selected)
        await app.set_props_and_fire(target, {}, event)
        assert app.find(f"agent-menu-{KEY}").open and not selected
        assert f"agent_menu open source={source}" in caplog.text
        assert "agent_menu shown" in caplog.text
        assert KEY not in caplog.text and "web" not in caplog.text

    logger = diagnostics_logger()
    logger.addHandler(caplog.handler)
    try:
        run(scenario())
    finally:
        logger.removeHandler(caplog.handler)


@pytest.mark.parametrize("width", [390, 1280])
def test_a_tap_still_selects_the_card_and_the_card_owns_the_hold(width: int) -> None:
    async def scenario() -> None:
        selected: list[str] = []
        app = await _render(width, Transport(), selected=selected)
        card = app.find(f"agent-{KEY}")
        assert isinstance(card, ft.TextButton) and card.on_click and card.on_long_press
        await app.set_props_and_fire(f"agent-{KEY}", {}, "click")
        assert selected == [KEY] and not _present(app, f"agent-menu-{KEY}")

    run(scenario())


def test_open_terminal_navigates_only_after_the_menu_is_patched_closed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from relaydeck.client.features.inventory import rename_presentation

    monkeypatch.setattr(rename_presentation, "CLOSE_FALLBACK_S", 0.4)

    async def scenario() -> None:
        order: list[str] = []
        app: Any = None

        def navigate(key: str) -> None:
            # The menu is already closed on screen when navigation happens.
            order.append(f"select:{key}:menu_open={app.find(f'agent-menu-{KEY}').open}")

        app = await _render(390, Transport(), selected=_Spy(navigate))
        await app.set_props_and_fire(f"agent-gesture-{KEY}", {}, "long_press_start")
        await app.set_props_and_fire(f"agent-open-{KEY}", {}, "click")
        assert not app.find(f"agent-menu-{KEY}").open and order == []
        await asyncio.sleep(0.8)  # no close report: the fallback navigates
        await app.settle()
        assert order == [f"select:{KEY}:menu_open=False"]

    run(scenario())


class _Spy(list):  # type: ignore[type-arg]
    def __init__(self, hook: Any) -> None:
        super().__init__()
        self._hook = hook

    def append(self, key: str) -> None:
        super().append(key)
        self._hook(key)
