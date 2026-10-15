"""In-app copy fallback with mandatory review, and per-attempt picture pickers."""

from __future__ import annotations

import gc
import logging
from typing import Any

import flet as ft
import pytest

from relaydeck.client.adapters.clipboard import (
    CLIPBOARD_READ_TIMEOUT,
    ClipboardReadStatus,
    FletClipboard,
    read_clipboard,
)
from relaydeck.client.features.terminal.input_modes import WORKFLOW_AGENT_DIRECT
from relaydeck.client.features.terminal.paste_review import CONFIRM_KEY
from relaydeck.client.features.terminal.pictures import (
    FAILURE,
    PictureClientError,
)
from relaydeck.client.features.terminal.surfaces import mount_agent_screen
from tests.support.host_harness import render_component, run
from tests.support.pictures.attachments import IDENTIFIER, PATH, _Api, choose, install_picker
from tests.support.pictures.clipboard_picker import _Blocked
from tests.support.terminal.direct_typing import _direct
from tests.support.terminal.latch_text import _inputs
from tests.support.terminal.sessions import _agent_session


class _Service:
    def __init__(self, reply: Any = "external", fail_set: bool = False) -> None:
        self.reply = reply
        self.fail_set = fail_set
        self.timeouts: list[float | None] = []

    async def _invoke_method(self, name: str, args: Any = None, timeout: Any = None) -> Any:
        assert name == "get"
        self.timeouts.append(timeout)
        if isinstance(self.reply, BaseException):
            raise self.reply
        return self.reply

    async def set(self, value: str) -> None:
        if self.fail_set:
            raise RuntimeError("set failed")


def test_platform_read_is_bounded_at_flets_invocation_boundary() -> None:
    async def scenario() -> None:
        service = _Service()
        port = FletClipboard(service)  # type: ignore[arg-type]
        assert await port.get_text() == "external"
        assert service.timeouts == [CLIPBOARD_READ_TIMEOUT]

    run(scenario())


@pytest.mark.parametrize("error", [TimeoutError(), RuntimeError("PlatformException")])
def test_failed_read_offers_in_app_copy_labelled(error: Exception) -> None:
    async def scenario() -> None:
        port = FletClipboard(_Service(error))  # type: ignore[arg-type]
        assert (await read_clipboard(port)).status is ClipboardReadStatus.UNAVAILABLE
        await port.set_text("copied here")
        result = await read_clipboard(port)
        assert result.status is ClipboardReadStatus.AVAILABLE
        assert result.text == "copied here" and result.from_app is True

    run(scenario())


@pytest.mark.parametrize("reply", ["", None, "fresh external"])
def test_successful_read_is_never_replaced_by_the_cache(reply: Any) -> None:
    async def scenario() -> None:
        port = FletClipboard(_Service(reply))  # type: ignore[arg-type]
        await port.set_text("copied here")
        result = await read_clipboard(port)
        assert result.text == reply and result.from_app is False

    run(scenario())


def test_failed_copy_leaves_no_cache_and_sessions_are_isolated() -> None:
    async def scenario() -> None:
        failing = FletClipboard(_Service(TimeoutError(), fail_set=True))  # type: ignore[arg-type]
        with pytest.raises(RuntimeError):
            await failing.set_text("never copied")
        assert failing.copied_text() is None
        assert (await read_clipboard(failing)).status is ClipboardReadStatus.UNAVAILABLE
        first = FletClipboard(_Service(TimeoutError()))  # type: ignore[arg-type]
        other = FletClipboard(_Service(TimeoutError()))  # type: ignore[arg-type]
        await first.set_text("page one")
        assert (await read_clipboard(other)).status is ClipboardReadStatus.UNAVAILABLE

    run(scenario())


@pytest.mark.parametrize("shell", [False, True])
def test_single_line_in_app_copy_waits_for_review_before_any_byte(shell: bool) -> None:
    async def scenario() -> None:
        app, session, socket, _ = await _direct(shell)
        try:
            session._clipboard = _Blocked("echo copied")
            await app.set_props_and_fire("shell-paste" if shell else "agent-paste", {}, "click")
            assert _inputs(socket) == []
            assert session.pending_review is not None
            assert session.pending_review.preview == "echo copied"
            await app.set_props_and_fire(CONFIRM_KEY, {}, "click")
            assert _inputs(socket) == [b"echo copied"]
        finally:
            await session.dispose()

    run(scenario())


async def _deliver(app: Any, picker: Any) -> None:
    await app.session.dispatch_event(
        picker._i, "result", {"files": [{"id": 7, "name": "a.jpg", "size": 100}]}
    )
    await app.settle()


@pytest.mark.parametrize("shell", [False, True])
def test_dismissed_pick_does_not_lock_and_late_result_is_ignored(
    shell: bool, monkeypatch: pytest.MonkeyPatch
) -> None:
    api = _Api()
    calls = install_picker(monkeypatch, api)

    async def scenario() -> None:
        app, session, socket, _ = await _direct(shell)
        try:
            key = "terminal-attach-picture"
            first = app.find(key).action.file_picker
            await app.set_props_and_fire(key, {}, "click")  # A: picker dismissed, no result
            await app.settle()
            button = app.find(key)
            assert not button.disabled
            second = button.action.file_picker
            assert second is not first
            await app.set_props_and_fire(key, {}, "click")  # B
            await app.settle()
            await _deliver(app, first)  # late A result
            assert calls == [] and _inputs(socket) == []
            await _deliver(app, second)
            assert len(calls) == 1 and _inputs(socket) == [PATH.encode()]
        finally:
            await session.dispose()

    run(scenario())


@pytest.mark.parametrize("new_workspace", [None, {"label": "qa"}])
def test_shell_create_is_accepted_by_the_pending_command_store(new_workspace: Any) -> None:
    from relaydeck.client.features.create.commands import shell_create_command
    from relaydeck.client.pending_commands import MemoryCommandStore, PendingCommands

    async def scenario() -> None:
        commands = PendingCommands(MemoryCommandStore(), "https://gw", "acct", "dev")
        await commands.load()
        body = shell_create_command(
            command_id="cmd_shell_create_01",
            host_id="home-mac",
            host_epoch="epoch-1",
            workspace_id="w2",
            label="qa-attach",
            new_workspace=new_workspace,
        )
        await commands.begin(body)
        assert ("home-mac", "cmd_shell_create_01") in commands.records

    run(scenario())


class _SlowApi(_Api):
    """Real gateway order: the result is still uploading until the PUT lands."""

    def __init__(self) -> None:
        super().__init__()
        self.landed = False
        self.reads: list[str] = []

    async def result(self, identifier: str) -> dict[str, Any]:
        doc = await super().result(identifier)
        status = "ready" if self.landed else "uploading"
        self.reads.append(status)
        return {**doc, "status": status, **({} if self.landed else {"path": ""})}


@pytest.mark.parametrize("shell", [False, True])
def test_web_upload_waits_for_completion_before_reading_the_result(
    shell: bool, monkeypatch: pytest.MonkeyPatch
) -> None:
    import asyncio

    import flet as ft

    from relaydeck.client.features.shell.ops import ShellGatewayOps
    from relaydeck.client.features.terminal import pictures

    api = _SlowApi()
    monkeypatch.setattr(ShellGatewayOps, "picture_api", property(lambda _: api))
    monkeypatch.setattr(pictures, "RESULT_POLL_SECONDS", 0.01)

    async def upload(self: Any, files: Any) -> None:
        # Like Flet web: upload() returns at once; progress arrives later.
        async def later() -> None:
            await asyncio.sleep(0.05)
            await self.page.session.dispatch_event(
                self._i, "upload", {"file_name": "a.jpg", "progress": 1.0, "error": None}
            )
            await asyncio.sleep(0.03)
            api.landed = True

        asyncio.get_running_loop().create_task(later())

    monkeypatch.setattr(ft.FilePicker, "upload", upload)

    async def scenario() -> None:
        app, session, socket, _ = await _direct(shell)
        try:
            key = "terminal-attach-picture"
            picker = app.find(key).action.file_picker
            await app.set_props_and_fire(key, {}, "click")
            await _deliver(app, picker)
            for _ in range(100):
                if _inputs(socket):
                    break
                await asyncio.sleep(0.01)
                await app.settle()
            assert _inputs(socket) == [PATH.encode()]
            assert api.reads[-1] == "ready" and api.cancelled == []
        finally:
            await session.dispose()

    run(scenario())


def test_compose_while_watching_attaches_picture_to_draft(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    api = _Api()
    calls = install_picker(monkeypatch, api)

    async def scenario() -> None:
        session, _ops, factory, _submitted = _agent_session()
        session.open()  # watching: disclosure armed, no attach, no keys
        assert session.keys_enabled is False
        app = await render_component(mount_agent_screen, session, phone=False)
        try:
            assert session.sessions.modes.text_edits_draft is True
            assert not app.find("composer-attach-picture").disabled
            await choose(app, "composer-attach-picture")
            await app.settle()
            assert len(calls) == 1
            assert api.cancelled == []
            assert [p.path for p in session.store.pictures] == [PATH]
            assert session.store.pictures[0].picture_id == IDENTIFIER
            assert factory.sockets == []
        finally:
            await session.dispose()

    run(scenario())


def test_direct_without_control_still_refuses_attach(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    api = _Api()
    calls = install_picker(monkeypatch, api)

    async def scenario() -> None:
        session, _ops, factory, _submitted = _agent_session()
        session.open()
        app = await render_component(mount_agent_screen, session, phone=False)
        try:
            session.set_workflow(WORKFLOW_AGENT_DIRECT)
            await app.settle()
            assert session.keys_enabled is False
            assert app.find("terminal-attach-picture").disabled
            await choose(app, "terminal-attach-picture")
            await app.settle()
            assert calls == [] and api.cancelled == []
            assert session.store.pictures == ()
            assert factory.sockets == []
        finally:
            await session.dispose()

    run(scenario())


_LEAK_MARKERS = (
    "bearer-leakprobe-7f3a9c",
    "grant-leakprobe-9d2b1e",
    "cookie-leakprobe-4a8f0d",
    "/Users/private/not-for-ui/leakprobe.jpg",
)


class _LeakyApi(_Api):
    """Raises caller-supplied errors from begin() to probe log hygiene."""

    def __init__(self, error: BaseException) -> None:
        super().__init__()
        self._error = error

    async def begin(self, host: str) -> dict[str, Any]:
        raise self._error


@pytest.mark.parametrize("shell", [False, True])
@pytest.mark.parametrize(
    ("kind", "reason"),
    [
        ("refused", "reason=PictureClientError"),
        ("timeout", "reason=TimeoutError"),
        ("unexpected", "reason=other"),
    ],
)
def test_upload_error_logs_carry_no_sensitive_text(
    shell: bool,
    kind: str,
    reason: str,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    if kind == "refused":
        error: BaseException = PictureClientError(
            f"refused bearer={_LEAK_MARKERS[0]} grant={_LEAK_MARKERS[1]}"
        )
        error.__cause__ = ValueError(f"cookie={_LEAK_MARKERS[2]} path={_LEAK_MARKERS[3]}")
    elif kind == "timeout":
        error = TimeoutError(f"timed out url=https://gw/x?grant={_LEAK_MARKERS[1]}")
        error.__cause__ = ConnectionError(f"cookie={_LEAK_MARKERS[2]}")
    else:
        error = RuntimeError(f"boom bearer={_LEAK_MARKERS[0]} path={_LEAK_MARKERS[3]}")
        error.__cause__ = ValueError(f"grant={_LEAK_MARKERS[1]} cookie={_LEAK_MARKERS[2]}")
    install_picker(monkeypatch, _LeakyApi(error))

    async def scenario() -> None:
        app, session, socket, _ = await _direct(shell)
        try:
            key = "terminal-attach-picture"
            picker = app.find(key).action.file_picker
            await app.set_props_and_fire(key, {}, "click")
            await _deliver(app, picker)
            assert _inputs(socket) == []
            if kind != "refused":
                assert app.find("picture-upload-message").value == FAILURE
        finally:
            await session.dispose()

    with caplog.at_level(logging.WARNING, logger="relaydeck.client.features.terminal.pictures"):
        run(scenario())
    assert reason in caplog.text
    for marker in _LEAK_MARKERS:
        assert marker not in caplog.text


_UNMOUNT_ATTRS = (
    "controls",
    "content",
    "destinations",
    "actions",
    "items",
    "leading",
    "title",
    "subtitle",
    "trailing",
)


def _collect_components(root: Any) -> list[Any]:
    """Every Component under root, mirroring the harness tree walk."""
    stack: list[Any] = [root]
    seen: set[int] = set()
    found: list[Any] = []
    while stack:
        control = stack.pop()
        if control is None or id(control) in seen:
            continue
        if isinstance(control, list | tuple):
            stack.extend(control)
            continue
        seen.add(id(control))
        if type(control).__name__ == "Component":
            found.append(control)
        body = getattr(control, "_b", None)
        if body is not None:
            stack.extend(body if isinstance(body, list) else [body])
        for attr in _UNMOUNT_ATTRS:
            try:
                value = getattr(control, attr, None)
            except Exception:  # noqa: BLE001 - tolerant tree walk
                continue
            if value is None:
                continue
            if isinstance(value, list | tuple):
                stack.extend(value)
            else:
                stack.append(value)
    return found


def _unmount_all(found: list[Any]) -> None:
    for comp in found:
        comp.will_unmount()


def test_abandoned_pickers_stay_bounded_and_unmount_clears(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    install_picker(monkeypatch, _Api())

    def count(app: Any) -> int:
        return len([s for s in app.page._services._services if isinstance(s, ft.FilePicker)])

    def steady_state(app: Any) -> None:
        # Collect dead render generations (continuous in production) and
        # run the same registry sweep the session performs after events.
        gc.collect()
        app.page._services.unregister_services()
        gc.collect()

    async def scenario() -> None:
        app, session, _socket, _ = await _direct(False)
        try:
            assert count(app) == 1
            key = "terminal-attach-picture"
            previous = None
            for _ in range(5):
                await app.set_props_and_fire(key, {}, "click")
                current = app.find(key).action.file_picker
                assert previous is None or current is not previous
                previous = current
            steady_state(app)
            # Live attempt + armed fresh picker + one renderer-pinned
            # generation; superseded services are retired, never reused.
            assert count(app) <= 3
            # Release the frame's picker handles before the unmount
            # half: they would otherwise root the live attempt across
            # gc and the sweep would legitimately keep it.
            del previous, current
            # Render replacement swaps the root body without running
            # lifecycle, so drive the router-pop equivalent explicitly:
            # collect the live components first (the swap detaches them),
            # replace, then run the public will_unmount of each. The walk
            # and bindings stay inside helpers so no test-frame local
            # pins the dead tree across gc.
            stale = _collect_components(app.page.views[0].controls)
            app.session.page.render(lambda: ft.Text("gone"))
            _unmount_all(stale)
            del stale
            await app.settle()
            # The session scheduler's suspended frame keeps its last
            # pass's loop bindings until its next pass; closing the
            # session (public API, clears no registry entries itself)
            # releases them. Production rebinds them on the next
            # update instead.
            app.session.close()
            await app.settle()
            steady_state(app)
            assert count(app) == 0
        finally:
            await session.dispose()

    run(scenario())
