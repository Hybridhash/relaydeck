"""Ctrl/Cmd+Enter sends the agent draft; Enter and Shift+Enter stay newlines; buttons align."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import flet as ft
import pytest

from relaydeck.client.features.composer import shortcut as shortcut_module
from relaydeck.client.features.composer.components import (
    DRAFT_KEY,
    FOOTER_KEY,
    SEND_KEY,
    ComposerBox,
)
from relaydeck.client.features.composer.domain import (
    BLOCKER_EMPTY,
    INPUT_MODE_COMPOSER,
    SUBMISSION_IDLE,
    SUBMISSION_SENDING,
)
from relaydeck.client.features.inventory.routing import RouteBridge
from relaydeck.client.features.terminal.paste_policy import PasteReview
from relaydeck.client.theme import MIN_TOUCH
from tests.support.host_harness import HostApp, render_component, run
from tests.support.terminal.direct_typing import open_compose
from tests.support.ui.retained_root import _active, _render


def _key(
    key: str = "Enter", *, ctrl: bool = False, meta: bool = False, shift: bool = False
) -> dict[str, Any]:
    return {"key": key, "ctrl": ctrl, "meta": meta, "shift": shift, "alt": False}


async def _press(app: HostApp, **kwargs: Any) -> None:
    await app.session.dispatch_event(app.page._i, "keyboard_event", _key(**kwargs))
    await app.settle()


def _props(sends: list[int], drafts: list[str], **over: Any) -> dict[str, Any]:
    props: dict[str, Any] = {
        "draft": "hello",
        "markers": (),
        "can_send": True,
        "blockers": (),
        "submission": SUBMISSION_IDLE,
        "error": None,
        "review": None,
        "input_mode": INPUT_MODE_COMPOSER,
        "on_draft_change": drafts.append,
        "on_send": lambda: sends.append(1),
        "on_confirm_blocked": lambda: None,
        "on_cancel_blocked": lambda: None,
        "on_clear_error": lambda: None,
    }
    props.update(over)
    return props


async def _focused_box(**over: Any) -> tuple[HostApp, list[int], list[str]]:
    sends: list[int] = []
    drafts: list[str] = []
    app = await render_component(ComposerBox, **_props(sends, drafts, **over))
    await app.set_props_and_fire(DRAFT_KEY, {}, "focus")
    return app, sends, drafts


@pytest.mark.parametrize("modifier", ["ctrl", "meta"])
def test_ctrl_or_cmd_enter_sends_once_and_swallows_the_newline_it_inserted(modifier: str) -> None:
    async def scenario() -> None:
        app, sends, drafts = await _focused_box()
        await _press(app, **{modifier: True})
        assert sends == [1]
        # The text field still inserts a newline for that Enter; it must not reach the draft.
        await app.set_props_and_fire(DRAFT_KEY, {"value": "hello\n"}, "change", "hello\n")
        assert drafts == []
        await app.set_props_and_fire(DRAFT_KEY, {"value": "hello\n!"}, "change", "hello\n!")
        assert drafts == ["hello\n!"]

    run(scenario())


def test_the_swallowed_newline_can_be_anywhere_in_the_draft() -> None:
    async def scenario() -> None:
        app, sends, drafts = await _focused_box(draft="ab")
        await _press(app, ctrl=True)
        await app.set_props_and_fire(DRAFT_KEY, {"value": "a\nb"}, "change", "a\nb")
        assert sends == [1] and drafts == []

    run(scenario())


def test_the_newline_left_after_the_send_cleared_the_field_is_swallowed() -> None:
    async def scenario() -> None:
        app, sends, drafts = await _focused_box()
        await _press(app, meta=True)
        await app.set_props_and_fire(DRAFT_KEY, {"value": "\n"}, "change", "\n")
        assert sends == [1] and drafts == []

    run(scenario())


def test_plain_enter_and_shift_combinations_never_send() -> None:
    async def scenario() -> None:
        app, sends, drafts = await _focused_box()
        await _press(app)
        await _press(app, shift=True)
        await _press(app, ctrl=True, shift=True)
        await _press(app, key="a", ctrl=True)
        assert sends == []
        await app.set_props_and_fire(DRAFT_KEY, {"value": "hello\n"}, "change", "hello\n")
        assert drafts == ["hello\n"]  # a plain Enter newline reaches the draft

    run(scenario())


@pytest.mark.parametrize(
    "over",
    [
        {"draft": "", "can_send": False, "blockers": (BLOCKER_EMPTY,)},
        {"submission": SUBMISSION_SENDING},
        {"review": PasteReview(line_count=2, character_count=9, preview="a\nb")},
    ],
)
def test_empty_draft_or_send_in_flight_or_review_does_nothing(over: dict[str, Any]) -> None:
    async def scenario() -> None:
        app, sends, _drafts = await _focused_box(**over)
        await _press(app, ctrl=True)
        assert sends == []

    run(scenario())


def test_an_unfocused_composer_ignores_the_shortcut() -> None:
    async def scenario() -> None:
        sends: list[int] = []
        app = await render_component(ComposerBox, **_props(sends, []))
        await _press(app, ctrl=True)
        await app.set_props_and_fire(DRAFT_KEY, {}, "focus")
        await app.set_props_and_fire(DRAFT_KEY, {}, "blur")
        await _press(app, ctrl=True)
        assert sends == []

    run(scenario())


def test_the_shortcut_is_released_when_the_composer_unmounts() -> None:
    async def scenario() -> None:
        @ft.component
        def Host() -> Any:
            mounted, set_mounted = ft.use_state(True)
            return ft.Column(
                [
                    ComposerBox(**_props([], [])) if mounted else ft.Text("gone"),
                    ft.Button("off", key="off", on_click=lambda e: set_mounted(False)),
                ]
            )

        app = await render_component(Host)
        assert app.page.on_keyboard_event is not None
        await app.set_props_and_fire("off", {}, "click")
        assert app.page.on_keyboard_event is None

    run(scenario())


@pytest.mark.parametrize(("platform", "hint"), [("macos", "⌘+Enter"), ("windows", "Ctrl+Enter")])
def test_send_tooltip_names_the_shortcut_for_the_platform(
    monkeypatch: pytest.MonkeyPatch, platform: str, hint: str
) -> None:
    async def scenario() -> None:
        monkeypatch.setattr(
            shortcut_module, "context", SimpleNamespace(page=SimpleNamespace(platform=platform))
        )
        sends: list[int] = []
        app = await render_component(ComposerBox, **_props(sends, []))
        assert app.find(SEND_KEY).tooltip == f"Send ({hint})"

    run(scenario())


def test_paperclip_and_send_share_size_axis_and_row() -> None:
    async def scenario() -> None:
        attach = ft.IconButton(ft.Icons.ATTACH_FILE, key="attach", width=32, height=36)
        app = await render_component(ComposerBox, **_props([], [], attachment=attach))
        send = app.find(SEND_KEY)
        clip = app.find("attach")
        assert (clip.width, clip.height) == (send.width, send.height) == (MIN_TOUCH, MIN_TOUCH)
        footer = app.find(FOOTER_KEY)
        assert clip in footer.controls and send in footer.controls
        assert footer.vertical_alignment == ft.CrossAxisAlignment.END
        assert clip.style.shape.radius == send.style.shape.radius
        assert clip.style.padding == send.style.padding

    run(scenario())


@pytest.mark.parametrize("width", [390, 768])
def test_the_phone_composer_sends_with_ctrl_enter_only(width: int) -> None:
    async def scenario() -> None:
        app, _controller, _sessions, pool, _now = await _render(
            bridge=RouteBridge(width=width, height=844), touch=True
        )
        try:
            await app.set_props_and_fire("agent-home-mac:term-1", {}, "click")
            await app.set_props_and_fire("agent-takeover-confirm", {}, "click")
            session = pool.foreground.session
            session.on_draft_change("from the keyboard")
            await open_compose(app)
            await app.set_props_and_fire("composer-draft", {}, "focus")
            assert _active(app, "composer-draft")
            before = len(session.store.messages)
            await _press(app)
            await _press(app, shift=True)
            assert len(session.store.messages) == before
            await _press(app, meta=True)
            assert len(session.store.messages) == before + 1
        finally:
            await app.set_props_and_fire("test-unmount", {}, "click")

    run(scenario())
