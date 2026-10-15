"""Picker completion survives draft-safe reconnects, never stale PTY writes."""

import asyncio
import logging
from typing import Any

import flet as ft
import pytest

from relaydeck.client.features.composer.pictures import Picture
from relaydeck.client.features.terminal import pictures
from relaydeck.client.features.terminal.input_modes import WORKFLOW_AGENT_DIRECT
from tests.support.host_harness import FakeConnection, render_component, run
from tests.support.pictures.attachments import IDENTIFIER, PATH, PREVIEW, _Api, install_picker
from tests.support.terminal.direct_typing import _direct
from tests.support.terminal.latch_text import _inputs
from tests.support.terminal.sessions import _agent_session


async def _result(app: Any, picker: Any, size: int = 100) -> None:
    await app.session.dispatch_event(
        picker._i,
        "result",
        {"files": [{"id": 7, "name": "private-name.jpg", "size": size}]},
    )
    await app.settle()


async def _change_boundary(app: Any, session: Any, change: str) -> None:
    if change == "controller":
        session._controller = None
        session._notify()
    elif change == "generation":
        session._interaction_generation += 1
        session._notify()
    elif change == "foreground":
        session.set_foreground(False)
        session.set_foreground(True)
    elif change == "reconnect":
        await session.reconnect()
    elif change == "flet_reconnect":
        # Pinned Flet attaches a replacement connection to the same Session;
        # ordinary verified reconnect does not recreate the Root/draft.
        conn = FakeConnection()
        conn.loop, conn.executor, conn.pubsubhub = (
            app.conn.loop,
            app.conn.executor,
            app.conn.pubsubhub,
        )
        await app.session.connect(conn)
        app.conn = conn
    await app.settle()


@pytest.mark.parametrize(
    "change", ["controller", "generation", "foreground", "reconnect", "flet_reconnect"]
)
def test_compose_pick_survives_connection_changes(
    change: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    api = _Api()
    calls = install_picker(monkeypatch, api)

    async def scenario() -> None:
        app, session, socket, _ = await _direct(False)
        try:
            await app.set_props_and_fire("agent-compose-toggle", {}, "click")
            picker = app.find("terminal-attach-picture").action.file_picker
            store, term = session.store, session.current_term
            await app.set_props_and_fire("terminal-attach-picture", {}, "click")
            await _change_boundary(app, session, change)
            await _result(app, picker)
            assert session.store is store and session.current_term is term
            assert session.store.draft == "retained compose draft"
            assert [picture.path for picture in store.pictures] == [PATH]
            assert len(calls) == 1 and not api.cancelled and not _inputs(socket)
        finally:
            await session.dispose()

    run(scenario())


@pytest.mark.parametrize("shell", [False, True])
def test_direct_changed_boundary_keeps_uploaded_picture_and_explains(
    shell: bool, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    api = _Api()
    calls = install_picker(monkeypatch, api)

    async def scenario() -> None:
        app, session, socket, _ = await _direct(shell)
        try:
            picker = app.find("terminal-attach-picture").action.file_picker
            await app.set_props_and_fire("terminal-attach-picture", {}, "click")
            await _change_boundary(app, session, "generation")
            await _result(app, picker)
            assert len(calls) == 1 and not api.cancelled and not _inputs(socket)
            assert app.find("picture-upload-message").value == (
                "Connection changed while picking; picture not inserted. Tap 📎 to try again."
            )
            if not shell:
                assert session.store.pictures == ()
                assert session.store.draft == "retained compose draft"
        finally:
            await session.dispose()

    with caplog.at_level(logging.INFO, logger="relaydeck.client.features.terminal.pictures"):
        run(scenario())
    assert "reason=boundary_changed" in caplog.text
    assert "private-name" not in caplog.text and IDENTIFIER not in caplog.text


@pytest.mark.parametrize("phase", ["grant", "upload", "poll"])
def test_compose_reconnect_at_every_upload_boundary(
    phase: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def scenario() -> None:
        app, session, socket, _ = await _direct(False)

        async def reconnect() -> None:
            session.set_foreground(False)
            await session.pause()
            await session.resume_after_pause()
            session.set_foreground(True)
            await app.settle()

        class Api(_Api):
            async def begin(self, host: str) -> dict[str, Any]:
                doc = await super().begin(host)
                if phase == "grant":
                    await reconnect()
                return doc

            async def result(self, identifier: str) -> dict[str, Any]:
                if phase == "poll":
                    await reconnect()
                return await super().result(identifier)

        api = Api()
        calls = install_picker(monkeypatch, api, reconnect if phase == "upload" else None)
        try:
            await app.settle()
            await app.set_props_and_fire("agent-compose-toggle", {}, "click")
            picker = app.find("terminal-attach-picture").action.file_picker
            before = len(session._ops.attaches)
            term, store = session.current_term, session.store
            await app.set_props_and_fire("terminal-attach-picture", {}, "click")
            await _result(app, picker)
            assert session.store is store and session.current_term is term
            assert [p.path for p in store.pictures] == [PATH]
            assert store.draft == "retained compose draft"
            assert len(calls) == 1 and not api.cancelled and not _inputs(socket)
            assert len(session._ops.attaches) == before  # no automatic takeover
        finally:
            await session.dispose()

    run(scenario())


@pytest.mark.parametrize("shell", [False, True])
@pytest.mark.parametrize("reason", ["not_foreground", "paused", "selection_open", "no_control"])
def test_direct_non_writer_boundaries_are_visible_and_never_insert(
    shell: bool, reason: str, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    api = _Api()
    install_picker(monkeypatch, api)

    async def scenario() -> None:
        app, session, socket, _ = await _direct(shell)

        async def change() -> None:
            if reason == "not_foreground":
                session.set_foreground(False)
            elif reason == "paused":
                session._paused = True
            elif reason == "selection_open":
                session.open_selection()
            else:
                session.controller.invalidate_writer("lease_expired")

        calls = install_picker(monkeypatch, api, change)
        await app.settle()
        try:
            picker = app.find("terminal-attach-picture").action.file_picker
            await app.set_props_and_fire("terminal-attach-picture", {}, "click")
            await _result(app, picker)
            assert app.find("picture-upload-message").value == pictures._EXIT_MESSAGES[reason]
            assert len(calls) == 1 and not api.cancelled and not _inputs(socket)
        finally:
            await session.dispose()

    with caplog.at_level(logging.INFO, logger=pictures.__name__):
        run(scenario())
    assert f"stage=insert reason={reason}" in caplog.text


def test_mode_change_during_direct_pick_never_stages_or_inserts(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    api = _Api()
    install_picker(monkeypatch, api)

    async def scenario() -> None:
        app, session, socket, _ = await _direct(False)
        try:
            picker = app.find("terminal-attach-picture").action.file_picker
            await app.set_props_and_fire("terminal-attach-picture", {}, "click")
            await app.set_props_and_fire("agent-compose-toggle", {}, "click")
            await _result(app, picker)
            assert (
                app.find("picture-upload-message").value == pictures._EXIT_MESSAGES["mode_changed"]
            )
            assert session.store.pictures == () and not api.cancelled and not _inputs(socket)
        finally:
            await session.dispose()

    with caplog.at_level(logging.INFO, logger=pictures.__name__):
        run(scenario())
    assert "reason=mode_changed" in caplog.text


@pytest.mark.parametrize("phase", ["start", "grant", "upload", "poll"])
@pytest.mark.parametrize("reason", ["revoked", "draft_changed", "session_changed", "disposed"])
def test_compose_identity_failures_stop_at_each_await_and_explain(
    phase: str, reason: str, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    async def scenario() -> None:
        app, session, socket, _ = await _direct(False)
        store = session.store

        async def invalidate() -> None:
            if reason == "revoked":
                session._on_renewal_terminal("unauthenticated")
            elif reason == "draft_changed":
                replacement, *_ = _agent_session()
                session._store = replacement.store
                await replacement.dispose()
            elif reason == "session_changed":
                session._agent_id = "different-agent"
            else:
                session._disposed = True

        class Api(_Api):
            async def begin(self, host: str) -> dict[str, Any]:
                doc = await super().begin(host)
                if phase == "grant":
                    await invalidate()
                return doc

            async def result(self, identifier: str) -> dict[str, Any]:
                doc = await super().result(identifier)
                if phase == "poll":
                    await invalidate()
                return doc

        api = Api()
        calls = install_picker(monkeypatch, api, invalidate if phase == "upload" else None)
        await app.settle()
        try:
            await app.set_props_and_fire("agent-compose-toggle", {}, "click")
            picker = app.find("terminal-attach-picture").action.file_picker
            await app.set_props_and_fire("terminal-attach-picture", {}, "click")
            if phase == "start":
                await invalidate()
            await _result(app, picker)
            assert app.find("picture-upload-message").value == pictures._EXIT_MESSAGES[reason]
            assert store.pictures == () and session.store.pictures == () and not _inputs(socket)
            assert api.cancelled == ([] if phase == "start" else [IDENTIFIER])
            assert len(calls) == (1 if phase in ("upload", "poll") else 0)
        finally:
            session._disposed = False
            await session.dispose()

    with caplog.at_level(logging.INFO, logger=pictures.__name__):
        run(scenario())
    assert f"stage={phase} reason={reason}" in caplog.text


@pytest.mark.parametrize("shell", [False, True])
def test_shell_and_agent_auth_loss_during_upload_cancel_without_input(
    shell: bool, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    api = _Api()
    install_picker(monkeypatch, api)

    async def scenario() -> None:
        app, session, socket, _ = await _direct(shell)

        async def revoke() -> None:
            session._on_renewal_terminal("forbidden")

        install_picker(monkeypatch, api, revoke)
        await app.settle()
        try:
            picker = app.find("terminal-attach-picture").action.file_picker
            await app.set_props_and_fire("terminal-attach-picture", {}, "click")
            await _result(app, picker)
            assert not _inputs(socket) and api.cancelled == [IDENTIFIER]
            assert app.find("picture-upload-message").value == pictures._EXIT_MESSAGES["revoked"]
        finally:
            await session.dispose()

    with caplog.at_level(logging.INFO, logger=pictures.__name__):
        run(scenario())
    assert "reason=revoked" in caplog.text


@pytest.mark.parametrize("reason", ["too_large", "stale_op", "retired_picker"])
def test_empty_oversized_or_stale_result_has_visible_fixed_reason(
    reason: str, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    api = _Api()
    calls = install_picker(monkeypatch, api)

    async def scenario() -> None:
        app, session, socket, _ = await _direct(False)
        try:
            picker = app.find("terminal-attach-picture").action.file_picker
            if reason != "stale_op":
                await app.set_props_and_fire("terminal-attach-picture", {}, "click")
            if reason == "retired_picker":
                await app.set_props_and_fire("terminal-attach-picture", {}, "click")
            await _result(app, picker, 10 * 1024 * 1024 + 1 if reason == "too_large" else 100)
            assert app.find("picture-upload-message").value == pictures._EXIT_MESSAGES[reason]
            assert not calls and not _inputs(socket) and not api.cancelled
        finally:
            await session.dispose()

    with caplog.at_level(logging.INFO, logger=pictures.__name__):
        run(scenario())
    assert f"reason={reason}" in caplog.text
    assert "Picture pick result: file_count=" in caplog.text


def test_picker_cancel_is_silent_but_logged(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    # item 7: cancelling the picker is a normal action: no
    # on-screen message, while the diagnostic log line is kept.
    api = _Api()
    calls = install_picker(monkeypatch, api)

    async def scenario() -> None:
        app, session, socket, _ = await _direct(False)
        try:
            picker = app.find("terminal-attach-picture").action.file_picker
            await app.set_props_and_fire("terminal-attach-picture", {}, "click")
            await app.session.dispatch_event(picker._i, "result", {"files": []})
            await app.settle()
            with pytest.raises(KeyError):
                app.find("picture-upload-message")
            assert not calls and not _inputs(socket) and not api.cancelled
        finally:
            await session.dispose()

    with caplog.at_level(logging.INFO, logger=pictures.__name__):
        run(scenario())
    assert "reason=no_files" in caplog.text
    assert "Picture pick result: file_count=" in caplog.text


@pytest.mark.parametrize("reason", ["cancelled", "upload_error", "not_ready", "picture_limit"])
def test_completion_failures_cancel_grant_and_explain(
    reason: str, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    async def scenario() -> None:
        app, session, socket, _ = await _direct(False)

        class Api(_Api):
            async def result(self, identifier: str) -> dict[str, Any]:
                doc = await super().result(identifier)
                if reason == "not_ready":
                    return {
                        **doc,
                        "status": "failed",
                        "message": "HEIC is unavailable; send a JPEG picture.",
                    }
                if reason == "picture_limit":
                    for i in range(8):
                        session.store.add_picture(Picture(f"{i:032x}", PATH, 99_999_999_999_999))
                return doc

        async def during_upload() -> None:
            await app.settle()
            if reason == "cancelled":
                await app.set_props_and_fire("picture-upload-cancel", {}, "click")
            elif reason == "upload_error":
                picker = original
                await app.session.dispatch_event(
                    picker._i,
                    "upload",
                    {"file_name": "private-name.jpg", "progress": None, "error": "secret-error"},
                )

        api = Api()
        install_picker(monkeypatch, api, during_upload)
        await app.settle()
        try:
            await app.set_props_and_fire("agent-compose-toggle", {}, "click")
            original = app.find("terminal-attach-picture").action.file_picker
            await app.set_props_and_fire("terminal-attach-picture", {}, "click")
            await _result(app, original)
            text = app.find("picture-upload-message").value
            assert text == (
                "HEIC is unavailable; send a JPEG picture."
                if reason == "not_ready"
                else pictures._EXIT_MESSAGES[reason]
            )
            assert not _inputs(socket) and api.cancelled
            assert all(p.picture_id != IDENTIFIER for p in session.store.pictures)
        finally:
            await session.dispose()

    with caplog.at_level(logging.INFO, logger=pictures.__name__):
        run(scenario())
    assert f"reason={reason}" in caplog.text
    assert "secret-error" not in caplog.text


@pytest.mark.parametrize("phase", ["upload", "insert"])
def test_duplicate_results_never_repeat_upload_or_insert(
    phase: str, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    api = _Api()

    async def scenario() -> None:
        session, _ops, _sockets, _submitted = _agent_session()
        session.open()
        await session.confirm_takeover()
        session.sessions.bind(session.controller)
        session.set_workflow(WORKFLOW_AGENT_DIRECT)
        inserted: list[Any] = []

        async def send(*args: Any, **kwargs: Any) -> str:
            inserted.append(args)
            if phase == "insert":
                await _result(app, picker)
            return "pty"

        @ft.component
        def View() -> Any:
            props = pictures.use_picture_attachment(session, send)
            return ft.Column(
                controls=[
                    pictures.attach_button(
                        props["on_attach_picture"],
                        enabled=props["attach_enabled"],
                        action=props["picture_action"],
                    ),
                    props["picture_progress"],
                ]
            )

        async def upload_duplicate() -> None:
            if phase == "upload":
                await _result(app, picker)

        calls = install_picker(monkeypatch, api, upload_duplicate)
        app = await render_component(View)
        try:
            picker = app.find("terminal-attach-picture").action.file_picker
            await app.set_props_and_fire("terminal-attach-picture", {}, "click")
            await _result(app, picker)
            assert len(calls) == len(inserted) == 1 and not api.cancelled
        finally:
            await session.dispose()

    with caplog.at_level(logging.INFO, logger=pictures.__name__):
        run(scenario())
    assert f"reason={'already_started' if phase == 'upload' else 'already_accepted'}" in caplog.text


def test_late_picker_never_mutates_replacement_session_or_its_notice(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    api = _Api()
    calls = install_picker(monkeypatch, api)

    async def scenario() -> None:
        first, *_ = _agent_session()
        replacement, *_ = _agent_session()
        replacement._agent_id = "agent-b"
        first.on_draft_change("first unsent")
        replacement.on_draft_change("replacement unsent")
        replacement.store.add_picture(Picture("b" * 32, PATH, 99_999_999_999_999, PREVIEW))
        first.open()
        replacement.open()

        @ft.component
        def View() -> Any:
            current, set_current = ft.use_state(first)
            props = pictures.use_picture_attachment(current, None)
            return ft.Column(
                controls=[
                    ft.TextButton(
                        "Switch", key="switch-session", on_click=lambda _: set_current(replacement)
                    ),
                    pictures.attach_button(
                        props["on_attach_picture"],
                        enabled=props["attach_enabled"],
                        action=props["picture_action"],
                    ),
                    props["picture_progress"],
                ]
            )

        app = await render_component(View)
        try:
            picker = app.find("terminal-attach-picture").action.file_picker
            await app.set_props_and_fire("terminal-attach-picture", {}, "click")
            await app.set_props_and_fire("switch-session", {}, "click")
            await _result(app, picker)
            assert first.store.pictures == () and first.store.draft == "first unsent"
            assert [p.picture_id for p in replacement.store.pictures] == ["b" * 32]
            assert replacement.store.draft == "replacement unsent" and not calls
            with pytest.raises(KeyError):
                app.find("picture-upload-message")
        finally:
            await first.dispose()
            await replacement.dispose()

    with caplog.at_level(logging.INFO, logger=pictures.__name__):
        run(scenario())
    assert "reason=retired_picker" in caplog.text


@pytest.mark.parametrize(
    ("size", "bucket"),
    [
        (100, "under_1mb"),
        (2_000_000, "under_5mb"),
        (7_000_000, "under_10mb"),
        (11_000_000, "over_10mb"),
    ],
)
def test_result_log_uses_only_count_and_size_bucket(
    size: int, bucket: str, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    install_picker(monkeypatch, _Api())

    async def scenario() -> None:
        app, session, _socket, _ = await _direct(False)
        try:
            picker = app.find("terminal-attach-picture").action.file_picker
            await app.set_props_and_fire("terminal-attach-picture", {}, "click")
            await app.session.dispatch_event(
                picker._i,
                "result",
                {
                    "files": [
                        {
                            "id": 7,
                            "name": "bearer-marker-grant-marker-cookie-marker.jpg",
                            "size": size,
                            "path": "/Users/private/path-marker?grant=grant-marker",
                            "bytes": b"image-content-marker",
                        }
                    ]
                },
            )
            await app.settle()
        finally:
            await session.dispose()

    with caplog.at_level(logging.INFO, logger=pictures.__name__):
        run(scenario())
    records = [r for r in caplog.records if r.name == pictures.__name__]
    assert [
        r.getMessage() for r in records if r.getMessage().startswith("Picture pick result:")
    ] == [f"Picture pick result: file_count=1 size_bucket={bucket}"]
    for marker in (
        "bearer-marker",
        "grant-marker",
        "cookie-marker",
        "path-marker",
        "image-content-marker",
        IDENTIFIER,
        PATH,
    ):
        assert marker not in caplog.text


def test_cancelled_completion_wait_is_visible_and_cancels_grant(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    api = _Api()
    install_picker(monkeypatch, api)

    async def scenario() -> None:
        app, session, socket, _ = await _direct(False)
        started = asyncio.Event()

        async def blocked_upload(self: Any, files: Any) -> None:
            started.set()
            await asyncio.Event().wait()

        monkeypatch.setattr(ft.FilePicker, "upload", blocked_upload)
        try:
            picker = app.find("terminal-attach-picture").action.file_picker
            await app.set_props_and_fire("terminal-attach-picture", {}, "click")
            task = asyncio.create_task(_result(app, picker))
            await started.wait()
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
            await app.settle()
            assert app.find("picture-upload-message").value == pictures._EXIT_MESSAGES["cancelled"]
            assert api.cancelled == [IDENTIFIER] and not _inputs(socket)
            assert not app.find("terminal-attach-picture").disabled
        finally:
            await session.dispose()

    with caplog.at_level(logging.INFO, logger=pictures.__name__):
        run(scenario())
    assert "stage=complete reason=cancelled" in caplog.text
