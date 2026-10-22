"""Real host controls, generation-bound writers and retained composer references."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import flet as ft
import pytest

from relaydeck.client.features.composer.pictures import Picture
from relaydeck.client.features.shell.ops import ShellGatewayOps
from relaydeck.client.features.shell.screen import mount_shell_screen
from relaydeck.client.features.terminal.input_modes import WORKFLOW_AGENT_DIRECT
from relaydeck.client.features.terminal.paste_policy import BRACKET_END, BRACKET_START
from relaydeck.client.features.terminal.pictures import (
    CONNECTION_CHANGED,
    FAILURE,
    PICTURE_MESSAGE_TTL,
)
from relaydeck.client.features.terminal.surfaces import mount_agent_screen
from tests.support.host_harness import render_component, run
from tests.support.pictures.attachments import (
    IDENTIFIER,
    PATH,
    PREVIEW,
    _Api,
    attach_control,
    choose,
    install_picker,
)
from tests.support.terminal.direct_typing import _direct, open_compose
from tests.support.terminal.latch_text import _inputs
from tests.support.terminal.sessions import _agent_session, _session
from tests.support.ui.phone_terminal import _ancestry


@pytest.mark.parametrize("shell", [False, True])
def test_native_picker_result_uses_authenticated_adapter(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, shell: bool
) -> None:
    import httpx

    from relaydeck.client.features.shell.ops import HttpxShellGatewayOps
    from relaydeck.companion.pictures import PictureStore
    from relaydeck.contracts.pairing import DEVICE_CAPABILITIES
    from relaydeck.gateway.app import create_app
    from tests.security.helpers import make_state
    from tests.support.pictures.uploads import picture

    async def scenario() -> None:
        state, _ = make_state()
        state.pictures = PictureStore(tmp_path / "private")
        token, _ = state.sessions.create(
            "a", DEVICE_CAPABILITIES, {"home-mac"}, state.now_ms(), device_id="one"
        )

        async def get_token() -> str:
            return token

        async def headerless_upload(*args: Any, **kwargs: Any) -> None:
            pytest.fail("Native upload must not use headerless FilePicker.upload")

        selected = tmp_path / "selected.png"
        selected.write_bytes(picture())
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=create_app(state)), base_url="https://mac.test"
        ) as client:
            api = HttpxShellGatewayOps(
                "https://mac.test", get_token, client, native_picture_upload=True
            ).picture_api
            monkeypatch.setattr(ShellGatewayOps, "picture_api", property(lambda _: api))
            monkeypatch.setattr(ft.FilePicker, "upload", headerless_upload)
            app, session, socket, _ = await _direct(shell)
            try:
                engine = session.current_term
                await choose(app, "terminal-attach-picture", selected)
                record = next(iter(state.pictures.uploads.values()))
                assert record.status == "ready" and record.device_id == "one"
                assert _inputs(socket) == [record.path.encode()]
                assert session.current_term is engine
                if not shell:
                    assert session.store.draft == "retained compose draft"
            finally:
                await session.dispose()

    run(scenario())


@pytest.mark.parametrize("shell", [False, True])
@pytest.mark.parametrize("bracketed", [False, True])
def test_explicit_picture_upload_inserts_exact_path_without_enter(
    shell: bool, bracketed: bool, monkeypatch: pytest.MonkeyPatch
) -> None:
    api = _Api()
    calls = install_picker(monkeypatch, api)

    async def scenario() -> None:
        app, session, socket, _ = await _direct(shell)
        try:
            terminal = session.current_term
            session.set_bracketed_paste(bracketed)
            button = attach_control(app)
            assert button.tooltip is None and not button.disabled
            assert any(
                isinstance(parent, ft.Semantics) and parent.label == "Attach picture"
                for parent in _ancestry(app, button)
            )
            assert _inputs(socket) == []
            await choose(app, "terminal-attach-picture")
            expected = PATH.encode()
            if bracketed:
                expected = BRACKET_START + expected + BRACKET_END
            assert _inputs(socket) == [expected]
            assert session.current_term is terminal and api.cancelled == []
            assert calls[0][0].method == "PUT"
            if not shell:
                assert session.store.draft == "retained compose draft"
        finally:
            await session.dispose()

    run(scenario())


def test_compose_chip_add_remove_roundtrip_and_explicit_send(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    api = _Api()
    install_picker(monkeypatch, api)

    async def scenario() -> None:
        app, session, socket, _ = await _direct(False)
        try:
            await open_compose(app)
            await choose(app, "terminal-attach-picture")
            assert len(session.store.pictures) == 1 and _inputs(socket) == []
            image = app.find(f"picture-chip-{IDENTIFIER}").controls[0]
            assert isinstance(image, ft.Image) and image.src == PREVIEW
            assert image.width == 48 and image.height == 48 and image.fit == ft.BoxFit.COVER
            doc = session.store.export_draft()
            session.store.import_draft(doc)
            assert session.store.pictures[0].path == PATH
            assert session.store.pictures[0].preview == PREVIEW
            assert session.store.export_draft()["pictures"][0]["preview"] == PREVIEW
            await app.set_props_and_fire(f"picture-remove-{IDENTIFIER}", {}, "click")
            assert session.store.pictures == () and _inputs(socket) == []
            await choose(app, "terminal-attach-picture")
            assert session.store.submission_text() == f"retained compose draft\n[image: {PATH}]"
            # Real Send callback emits one command, not picker-time prompt input.
            sent: list[dict[str, Any]] = []

            async def submit(command: Any) -> Any:
                sent.append(command)
                return {"command_id": command["command_id"], "status": "succeeded", "result": {}}

            session.store._submit_command = submit
            session.store._prompt_text = None
            session.store._observe_command = None
            send = app.find("composer-draft").suffix_icon
            await app.session.dispatch_event(send._i, "click", "")
            await app.settle()
            assert sent[0]["payload"]["text"] == f"retained compose draft\n[image: {PATH}]"
        finally:
            await session.dispose()

    run(scenario())


@pytest.mark.parametrize("shell", [False, True])
def test_upload_failure_sanitized_and_lost_generation_never_inserts(
    shell: bool, monkeypatch: pytest.MonkeyPatch
) -> None:
    api = _Api()
    install_picker(monkeypatch, api)

    async def scenario() -> None:
        app, session, socket, _ = await _direct(shell)
        try:
            api.failure = True
            await choose(app, "terminal-attach-picture")
            assert app.find("picture-upload-message").value == FAILURE
            api.failure = False

            async def replace() -> None:
                session._interaction_generation += 1

            install_picker(monkeypatch, api, replace)
            await choose(app, "terminal-attach-picture")
            assert _inputs(socket) == [] and api.cancelled == []
            assert app.find("picture-upload-message").value == CONNECTION_CHANGED
        finally:
            await session.dispose()

    run(scenario())


def test_watch_disables_attachment_and_forged_click_is_inert(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    api = _Api()
    calls = install_picker(monkeypatch, api)

    async def scenario() -> None:
        app, session, socket, _ = await _direct(False)
        try:
            attach = attach_control(app)
            forged_click, action = attach.on_click, attach.action
            await session.disconnect()
            await app.settle()
            for key in ("terminal-tools-button", "tools-attach-picture"):
                with pytest.raises(KeyError):
                    app.find(key)
            await forged_click(None)
            await app.session.dispatch_event(
                action.file_picker._i,
                "result",
                {"files": [{"id": 7, "name": "spoof.exe", "size": 100}]},
            )
            await app.settle()
            assert calls == [] and _inputs(socket) == []
        finally:
            await session.dispose()

    run(scenario())


@pytest.mark.parametrize("shell", [False, True])
def test_progress_cancel_action_never_inserts_and_keeps_terminal(
    shell: bool, monkeypatch: pytest.MonkeyPatch
) -> None:
    api = _Api()
    install_picker(monkeypatch, api)

    async def scenario() -> None:
        app, session, socket, _ = await _direct(shell)
        terminal = session.current_term
        try:

            async def cancel() -> None:
                await app.settle()
                assert app.find("picture-upload-cancel") is not None
                await app.set_props_and_fire("picture-upload-cancel", {}, "click")

            install_picker(monkeypatch, api, cancel)
            await choose(app, "terminal-attach-picture")
            assert _inputs(socket) == [] and api.cancelled
            assert session.current_term is terminal
            assert app.find("picture-upload-message").value.startswith("Picture upload cancelled")
        finally:
            await session.dispose()

    run(scenario())


def test_expired_picture_blocks_send_and_old_draft_snapshot_remains_compatible() -> None:
    async def scenario() -> None:
        _app, session, _socket, _ = await _direct(False)
        try:
            session.store.add_picture(Picture(IDENTIFIER, PATH, 0))
            assert not session.store.can_send
            assert any("picture-expired" in item for item in session.store.send_blockers())
            session.store.import_draft({"draft": "old draft", "markers": []})
            assert session.store.pictures == () and session.store.draft == "old draft"
        finally:
            await session.dispose()

    run(scenario())


@pytest.mark.parametrize("unsafe", ["\x1b", "\x00", "\n", "\x85", "\ud800"])
def test_picture_reference_rejects_terminal_controls(unsafe: str) -> None:
    with pytest.raises(ValueError):
        Picture.from_wire({"picture_id": IDENTIFIER, "path": PATH + unsafe, "expires_ms": 0})


@pytest.mark.parametrize("shell", [False, True])
def test_expanded_toolbar_inserts_only_path(shell: bool, monkeypatch: pytest.MonkeyPatch) -> None:
    api = _Api()
    install_picker(monkeypatch, api)

    async def scenario() -> None:
        if shell:
            session, _ops, sockets = _session()
            await session.open("term-1")
            app = await render_component(
                mount_shell_screen,
                session,
                phone=False,
                width_tier="expanded",
                on_back=lambda: None,
                on_new_terminal=lambda: None,
            )
        else:
            session, _ops, sockets, _submitted = _agent_session()
            session.open()
            await session.confirm_takeover()
            session.set_workflow(WORKFLOW_AGENT_DIRECT)
            app = await render_component(
                mount_agent_screen, session, phone=False, width_tier="expanded"
            )
        try:
            app.session.apply_patch(app.page._i, {"width": 1280, "height": 844})
            await choose(app, "terminal-attach-picture")
            assert _inputs(sockets.sockets[0]) == [PATH.encode()]
        finally:
            await session.dispose()

    run(scenario())


def test_picture_message_ttl_constant_is_auto_clear_window() -> None:
    assert PICTURE_MESSAGE_TTL == 5.0


def test_picture_message_auto_clears_after_ttl_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from relaydeck.client.features.terminal import pictures as pictures_mod

    monkeypatch.setattr(pictures_mod, "PICTURE_MESSAGE_TTL", 0.2)
    api = _Api()
    install_picker(monkeypatch, api)

    async def scenario() -> None:
        app, session, _socket, _ = await _direct(False)
        try:
            api.failure = True
            await choose(app, "terminal-attach-picture")
            assert app.find("picture-upload-message").value == FAILURE
            await asyncio.sleep(0.3)
            await app.settle()
            with pytest.raises(KeyError):
                app.find("picture-upload-message")
        finally:
            await session.dispose()

    run(scenario())


def test_identical_repeated_message_rearms_full_lifetime(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from relaydeck.client.features.terminal import pictures as pictures_mod

    monkeypatch.setattr(pictures_mod, "PICTURE_MESSAGE_TTL", 1.0)
    api = _Api()
    install_picker(monkeypatch, api)

    async def scenario() -> None:
        app, session, _socket, _ = await _direct(False)
        try:
            api.failure = True
            await choose(app, "terminal-attach-picture")
            assert app.find("picture-upload-message").value == FAILURE
            await asyncio.sleep(0.6)
            # Same text again: the old deadline must not clear the new notice.
            await choose(app, "terminal-attach-picture")
            await asyncio.sleep(0.6)
            await app.settle()
            assert app.find("picture-upload-message").value == FAILURE
            await asyncio.sleep(1.0)
            await app.settle()
            with pytest.raises(KeyError):
                app.find("picture-upload-message")
        finally:
            await session.dispose()

    run(scenario())
