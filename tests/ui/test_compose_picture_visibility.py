"""Phone attachment gestures reveal the draft without terminal side effects."""

import asyncio
from typing import Any

import flet as ft
import pytest
from flet.messaging.protocol import MessageAction

from relaydeck.client.features.composer.pictures import Picture
from relaydeck.client.features.inventory.routing import RouteBridge
from relaydeck.client.features.lifecycle.persist import draft_key, pause_persist
from relaydeck.client.features.terminal.input_modes import (
    WORKFLOW_AGENT_COMPOSER,
    WORKFLOW_AGENT_DIRECT,
)
from relaydeck.client.features.terminal.surfaces import mount_agent_screen
from tests.support.fakes import FakeCredentialStore
from tests.support.host_harness import render_component, run
from tests.support.pictures.attachments import (
    IDENTIFIER,
    PATH,
    PREVIEW,
    _Api,
    choose,
    install_picker,
)
from tests.support.sync.transport_fixtures import _wait
from tests.support.terminal.direct_typing import open_compose
from tests.support.terminal.sessions import _agent_session
from tests.support.ui.retained_root import _active, _render


@pytest.mark.parametrize("width", [360, 390])
@pytest.mark.parametrize("in_control", [False, True])
def test_staging_compose_picture_from_reading_reveals_phone_draft(
    width: int, in_control: bool, monkeypatch: pytest.MonkeyPatch
) -> None:
    install_picker(monkeypatch, _Api())

    async def scenario() -> None:
        session, ops, sockets, submitted = _agent_session()
        session.on_draft_change("exact unsent\ntext 😀")
        session.open()
        if in_control:
            await session.confirm_takeover()
        app = await render_component(
            mount_agent_screen, session, phone=True, width_tier="compact", viewport_height=844
        )
        app.session.apply_patch(app.page._i, {"width": width, "height": 844})
        try:
            if not in_control:
                with pytest.raises(KeyError):
                    _active(app, "composer-draft")
                # Attach lives in the tools popover, which needs control: while only
                # watching there is no way to stage a picture and the draft stays hidden.
                with pytest.raises(KeyError):
                    app.find("terminal-tools-button")
                assert session.store.pictures == ()
                return
            terminal = session.current_term
            token = session.input_token
            before = (len(ops.attaches), len(ops.leases), len(sockets.sockets))
            await choose(app, "terminal-attach-picture")
            assert session.sessions.modes.workflow == WORKFLOW_AGENT_COMPOSER
            assert session.store.draft == "exact unsent\ntext 😀"
            assert len(session.store.pictures) == 1
            image = _active(app, f"picture-chip-{IDENTIFIER}").controls[0]
            assert image.src == PREVIEW
            field = _active(app, "composer-draft")
            assert field.value == session.store.draft and field.tooltip is None
            assert session.current_term is terminal and session.input_token == token
            assert before == (len(ops.attaches), len(ops.leases), len(sockets.sockets))
            assert not submitted
            assert all(not socket.sent_bytes for socket in sockets.sockets)
            assert not any(
                msg.action == MessageAction.INVOKE_METHOD and msg.body.name == "focus"
                for msg in app.conn.sent
            )
        finally:
            await session.dispose()

    run(scenario())


class _FreshApi(_Api):
    async def begin(self, host: str) -> dict[str, Any]:
        return {**await super().begin(host), "picture_id": "b" * 32}

    async def result(self, identifier: str) -> dict[str, Any]:
        return {**await super().result(identifier), "picture_id": "b" * 32}


@pytest.mark.parametrize("delay", [0.0, 0.2])
def test_delayed_root_draft_restore_stays_reading_then_fresh_attach_reveals(
    delay: float, monkeypatch: pytest.MonkeyPatch
) -> None:
    install_picker(monkeypatch, _FreshApi())

    async def scenario() -> None:
        class Storage(FakeCredentialStore):
            async def get(self, key: str) -> str | None:
                await asyncio.sleep(delay)
                return await super().get(key)

        storage = Storage()
        sample, *_ = _agent_session()
        sample.store.add_picture(Picture(IDENTIFIER, PATH, 99_999_999_999_999, PREVIEW))
        sample.on_draft_change("securely restored\ntext 😀")
        await pause_persist(storage, draft_key("home-mac:term-1"), sample.store.export_draft())
        await sample.dispose()
        bridge = RouteBridge(width=390, height=844)
        app, _controller, scripted, pool, _now = await _render(bridge=bridge)
        scripted.factory.draft_store = storage
        try:
            await app.set_props_and_fire("agent-home-mac:term-1", {}, "click")
            session = pool.foreground.session
            terminal = session.current_term
            await _wait(
                lambda: session.last_resume_restore is not None and not session.draft_loading
            )
            await app.settle()
            assert len(session.store.pictures) == 1
            assert session.store.draft == "securely restored\ntext 😀"
            with pytest.raises(KeyError):
                _active(app, "composer-draft")
            # A later compact/medium layout notification cannot replay restore.
            for width in (768, 390):
                bridge.set_width(width)
                await app.settle()
                with pytest.raises(KeyError):
                    _active(app, "composer-draft")
            # The tools popover needs control, so take it first; the attach itself then
            # must neither take control again nor open another stream.
            await app.set_props_and_fire("agent-takeover-confirm", {}, "click")
            counts = (
                len(scripted.ops.attaches),
                len(scripted.ops.leases),
                len(scripted.streams.opened),
            )
            await choose(app, "terminal-attach-picture")
            assert _active(app, "picture-chip-" + "b" * 32).controls[0].src == PREVIEW
            assert _active(app, "composer-draft").value == "securely restored\ntext 😀"
            assert session.current_term is terminal
            assert counts == (
                len(scripted.ops.attaches),
                len(scripted.ops.leases),
                len(scripted.streams.opened),
            )
            assert not scripted.submitted
            assert not any(
                msg.action == MessageAction.INVOKE_METHOD and msg.body.name == "focus"
                for msg in app.conn.sent
            )
        finally:
            await app.set_props_and_fire("test-unmount", {}, "click")

    run(scenario())


@pytest.mark.parametrize("delay", [0.0, 0.1])
def test_prepare_draft_after_component_mount_does_not_reveal(delay: float) -> None:
    async def scenario() -> None:
        class Storage(FakeCredentialStore):
            async def get(self, key: str) -> str | None:
                await asyncio.sleep(delay)
                return await super().get(key)

        session, ops, sockets, submitted = _agent_session()
        storage = Storage()
        doc = session.store.export_draft()
        doc["pictures"] = [Picture(IDENTIFIER, PATH, 99_999_999_999_999, PREVIEW).document()]
        await pause_persist(storage, draft_key("home-mac:agent-1"), doc)
        session._draft_store = storage
        app = await render_component(mount_agent_screen, session, phone=True)
        try:
            await session.prepare_draft()
            await app.settle()
            assert session.store.pictures[0].picture_id == IDENTIFIER
            with pytest.raises(KeyError):
                _active(app, "composer-draft")
            assert not ops.attaches and not ops.leases and not sockets.sockets and not submitted
        finally:
            await session.dispose()

    run(scenario())


@pytest.mark.parametrize("case", ["initial", "remove", "direct", "background"])
def test_suppressed_picture_additions_cannot_reveal_later(
    case: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    from relaydeck.client.features.terminal.surfaces import direct_typing

    now = [0.0]
    monkeypatch.setattr(direct_typing, "_typing_now", lambda: now[0])

    async def scenario() -> None:
        session, ops, sockets, submitted = _agent_session()
        picture = Picture(IDENTIFIER, PATH, 99_999_999_999_999, PREVIEW)
        if case in {"initial", "remove"}:
            session.store.add_picture(picture)
        if case == "direct":
            session.open()
            await session.confirm_takeover()

        @ft.component
        def Parent() -> Any:
            tier, set_tier = ft.use_state("compact")
            return ft.Column(
                controls=[
                    mount_agent_screen(session, phone=True, width_tier=tier),
                    ft.Button("Medium", key="tier-medium", on_click=lambda e: set_tier("medium")),
                ]
            )

        app = await render_component(Parent)
        try:
            if case == "direct":
                await app.set_props_and_fire("agent-workflow-toggle", {}, "click")
                assert session.sessions.modes.workflow == WORKFLOW_AGENT_DIRECT
            elif case == "background":
                session.set_foreground(False)
                await app.settle()
            before = (len(ops.attaches), len(ops.leases), len(sockets.sockets))
            start = len(app.conn.sent)
            if case == "remove":
                session.store.remove_picture(IDENTIFIER)
            elif case in {"direct", "background"}:
                session.store.add_picture(picture)
            session.on_draft_change(session.store.draft)
            await app.settle()
            if case == "direct":
                assert session.sessions.modes.workflow == WORKFLOW_AGENT_DIRECT
                now[0] = 1.0
                await app.set_props_and_fire("agent-workflow-toggle", {}, "click")
                session.set_workflow(WORKFLOW_AGENT_COMPOSER)
                await app.settle()
            elif case == "background":
                session.set_foreground(True)
                await app.settle()
            await app.set_props_and_fire("tier-medium", {}, "click")
            if case != "direct":  # leaving Direct input shows the idle text box, unfocused
                with pytest.raises(KeyError):
                    _active(app, "composer-draft")
            assert before == (len(ops.attaches), len(ops.leases), len(sockets.sockets))
            assert not submitted and all(not socket.sent_bytes for socket in sockets.sockets)
            assert not any(
                msg.action == MessageAction.INVOKE_METHOD and msg.body.name == "focus"
                for msg in app.conn.sent[start:]
            )
        finally:
            await session.dispose()

    run(scenario())


def test_tools_opened_compact_compose_field_has_no_tooltip() -> None:
    async def scenario() -> None:
        session, _ops, _sockets, _submitted = _agent_session()
        session.open()
        await session.confirm_takeover()  # the tools popover needs control
        app: Any = await render_component(mount_agent_screen, session, phone=True)
        try:
            await open_compose(app)
            assert _active(app, "composer-draft").tooltip is None
        finally:
            await session.dispose()

    run(scenario())
