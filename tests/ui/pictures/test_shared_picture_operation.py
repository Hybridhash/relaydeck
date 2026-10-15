"""The picker and a non-picker source go through one operation with the same results."""

from __future__ import annotations

import asyncio
from typing import Any

import flet as ft
import pytest

from relaydeck.client.features.terminal.input_modes import WORKFLOW_AGENT_DIRECT
from relaydeck.client.features.terminal.pictures import attach_button, use_picture_attachment
from tests.support.host_harness import render_component, run
from tests.support.pictures.attachments import IDENTIFIER, PATH, _Api, choose, install_picker
from tests.support.pictures.paste_source import FakePasteSource
from tests.support.terminal.sessions import _agent_session


@ft.component
def _Probe(session: Any, send: Any, sink: dict[str, Any]) -> Any:
    props = use_picture_attachment(session, send, expose_sources=True)
    sink["props"] = props
    return ft.Column(
        controls=[
            attach_button(
                props["on_attach_picture"],
                enabled=props["attach_enabled"],
                action=props["picture_action"],
            ),
            props["picture_progress"],
        ]
    )


async def _render(direct: bool) -> tuple[Any, Any, list[bytes], dict[str, Any]]:
    session, _ops, _factory, _submitted = _agent_session()
    session.open()
    await session.confirm_takeover()
    if direct:
        session.sessions.modes.set_workflow(WORKFLOW_AGENT_DIRECT)
    sent: list[bytes] = []

    async def send(origin: Any, chunks: Any, **_kwargs: Any) -> str:
        sent.append(b"".join(chunks))
        return "pty"

    sink: dict[str, Any] = {}
    app = await render_component(_Probe, session, send, sink)
    return app, session, sent, sink


async def _paste(sink: dict[str, Any], source: FakePasteSource) -> str:
    props = sink["props"]
    return str(await props["attach_picture_source"](source, props["capture_picture_activation"]()))


def test_composer_stages_the_same_picture_from_the_picker_and_a_paste_source(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def scenario() -> None:
        api = _Api()
        install_picker(monkeypatch, api)
        app, session, sent, sink = await _render(direct=False)
        try:
            await choose(app, "terminal-attach-picture")
            (picked,) = session.store.pictures
            session.store.remove_picture(picked.picture_id)
            source = FakePasteSource()
            source.release.set()
            assert await _paste(sink, source) == "started"
            await app.settle()
            (pasted,) = session.store.pictures
            assert (picked.picture_id, picked.path, picked.preview) == (
                pasted.picture_id,
                pasted.path,
                pasted.preview,
            )
            assert picked.path == PATH and picked.picture_id == IDENTIFIER
            assert sent == [] and api.cancelled == []
        finally:
            await session.dispose()

    run(scenario())


def test_direct_inserts_the_same_bytes_from_the_picker_and_a_paste_source(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def scenario() -> None:
        api = _Api()
        install_picker(monkeypatch, api)
        app, session, sent, sink = await _render(direct=True)
        try:
            await choose(app, "terminal-attach-picture")
            source = FakePasteSource()
            source.release.set()
            assert await _paste(sink, source) == "started"
            await app.settle()
            assert sent == [PATH.encode(), PATH.encode()]
            assert session.store.pictures == ()
            assert api.cancelled == []
        finally:
            await session.dispose()

    run(scenario())


def test_a_paste_source_shares_the_picker_busy_gate_and_is_never_queued(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def scenario() -> None:
        api = _Api()
        install_picker(monkeypatch, api)
        app, session, _sent, sink = await _render(direct=False)
        try:
            first = FakePasteSource()
            running = asyncio.create_task(_paste(sink, first))
            await first.entered.wait()
            second = FakePasteSource()
            second.release.set()
            assert await _paste(sink, second) == "busy"
            assert second.uploads == 0 and second.disposed == 1
            first.release.set()
            assert await running == "started"
            await app.settle()
            assert len(session.store.pictures) == 1
        finally:
            await session.dispose()

    run(scenario())


def test_a_paste_source_is_refused_after_the_session_is_disposed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def scenario() -> None:
        api = _Api()
        install_picker(monkeypatch, api)
        app, session, sent, sink = await _render(direct=False)
        props = sink["props"]
        activation = props["capture_picture_activation"]()
        await session.dispose()
        source = FakePasteSource()
        source.release.set()
        assert await props["attach_picture_source"](source, activation) == "disposed"
        assert source.uploads == 0 and sent == [] and api.cancelled == []

    run(scenario())


class _CountingApi(_Api):
    def __init__(self) -> None:
        super().__init__()
        self.grants = 0

    async def begin(self, host: str) -> dict[str, Any]:
        self.grants += 1
        return await super().begin(host)


def test_delivering_the_same_captured_activation_twice_is_one_request(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def scenario() -> None:
        api = _CountingApi()
        install_picker(monkeypatch, api)
        app, session, sent, sink = await _render(direct=True)
        try:
            props = sink["props"]
            activation = props["capture_picture_activation"]()
            first = FakePasteSource()
            first.release.set()
            assert await props["attach_picture_source"](first, activation) == "started"
            again = FakePasteSource()
            again.release.set()
            assert await props["attach_picture_source"](again, activation) == "already_started"
            assert api.grants == 1 and again.uploads == 0 and again.disposed == 1
            assert sent == [PATH.encode()]
            # A new gesture is a new request even with identical pixels.
            fresh = FakePasteSource()
            fresh.release.set()
            props = sink["props"]
            assert (
                await props["attach_picture_source"](fresh, props["capture_picture_activation"]())
                == "started"
            )
            assert api.grants == 2 and len(sent) == 2
        finally:
            await session.dispose()

    run(scenario())


def test_an_activation_captured_before_the_draft_was_replaced_is_refused(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def scenario() -> None:
        import copy

        api = _CountingApi()
        install_picker(monkeypatch, api)
        app, session, sent, sink = await _render(direct=False)
        try:
            activation = sink["props"]["capture_picture_activation"]()
            original = session.store
            session._store = copy.copy(original)
            session._notify()
            await app.settle()
            source = FakePasteSource()
            source.release.set()
            outcome = await sink["props"]["attach_picture_source"](source, activation)
            assert outcome == "draft_changed"
            assert api.grants == 0 and source.uploads == 0 and source.disposed == 1
            assert len(original.pictures) == 0 and len(session.store.pictures) == 0
            assert sent == []
        finally:
            await session.dispose()

    run(scenario())


def test_a_draft_replaced_during_the_upload_stops_staging_and_deletes_the_grant(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def scenario() -> None:
        import copy

        api = _CountingApi()
        install_picker(monkeypatch, api)
        app, session, sent, sink = await _render(direct=False)
        try:
            props = sink["props"]
            source = FakePasteSource()
            running = asyncio.create_task(
                props["attach_picture_source"](source, props["capture_picture_activation"]())
            )
            await source.entered.wait()
            original = session.store
            session._store = copy.copy(original)
            session._notify()
            source.release.set()
            await running
            await app.settle()
            assert len(original.pictures) == 0 and len(session.store.pictures) == 0
            assert api.cancelled == [IDENTIFIER]
        finally:
            await session.dispose()

    run(scenario())


def _region_handler(sink: dict[str, Any]) -> Any:
    from relaydeck.client.features.terminal.picture_paste_region import (
        take_paste_props,
        wrap_with_paste_region,
    )

    paste = take_paste_props(dict(sink["props"]))
    return wrap_with_paste_region(object(), paste, key="r")


class _Control:
    def __init__(self) -> None:
        self.aborts: list[str] = []

    async def abort(self, request_id: str) -> None:
        self.aborts.append(request_id)

    async def upload(self, request_id: str, url: str, wait_ms: int) -> str:
        return "ok"


async def _deliver(sink: dict[str, Any], binding: str, control: _Control, request_id: str) -> None:
    from types import SimpleNamespace

    region = _region_handler(sink)
    event = SimpleNamespace(
        request_id=request_id, size=100, mime="image/png", binding=binding, control=control
    )
    await region.on_offer(event)
    await asyncio.sleep(0)


@pytest.mark.parametrize("change", ["workflow", "draft", "reconnect"])
def test_an_offer_delayed_across_a_change_is_refused_not_rebound(
    change: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def scenario() -> None:
        import copy

        api = _CountingApi()
        install_picker(monkeypatch, api)
        app, session, sent, sink = await _render(direct=False)
        try:
            gesture_binding = sink["props"]["picture_paste_binding"]
            if change == "workflow":
                session.sessions.modes.set_workflow(WORKFLOW_AGENT_DIRECT)
            elif change == "draft":
                session._store = copy.copy(session.store)
            else:
                session._interaction_generation += 1
            session._notify()
            await app.settle()
            control = _Control()
            await _deliver(sink, gesture_binding, control, "p1-1")
            assert api.grants == 0 and sent == []
            assert len(session.store.pictures) == 0
            assert control.aborts == ["p1-1"]
        finally:
            await session.dispose()

    run(scenario())


def test_an_offer_with_the_binding_of_the_gesture_attaches(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def scenario() -> None:
        api = _CountingApi()
        install_picker(monkeypatch, api)
        app, session, sent, sink = await _render(direct=False)
        try:
            control = _Control()
            await _deliver(sink, sink["props"]["picture_paste_binding"], control, "p2-1")
            for _ in range(40):
                if session.store.pictures:
                    break
                await asyncio.sleep(0.05)
            assert api.grants == 1 and len(session.store.pictures) == 1 and sent == []
        finally:
            await session.dispose()

    run(scenario())


@pytest.mark.parametrize(
    ("reason", "text"),
    [
        ("busy", "A picture is already uploading. Wait for it to finish."),
        ("stale", "The screen changed while pasting; picture not attached. Paste it again."),
        ("multiple", "Paste one picture at a time."),
    ],
)
def test_a_refused_paste_explains_itself(
    reason: str, text: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def scenario() -> None:
        install_picker(monkeypatch, _Api())
        app, session, _sent, sink = await _render(direct=False)
        try:
            sink["props"]["reject_picture_paste"](reason)
            await app.settle()
            assert app.find("picture-upload-message").value == text
        finally:
            await session.dispose()

    run(scenario())
