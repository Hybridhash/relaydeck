"""Authenticated UI transport continuity through pinned Flet's real ASGI handler."""

from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime, timedelta
from typing import Any

import flet as ft
import msgpack  # type: ignore[import-untyped]
import pytest
from flet.fastapi import app_manager
from starlette.websockets import WebSocketDisconnect

from relaydeck.client.serve import (
    COOKIE_NAME,
    FletGuardMiddleware,
    WebSessionStore,
    build_web_app,
    rebind_on_connect,
    teardown_web_sid,
)
from relaydeck.gateway.app import GatewayState, create_app
from relaydeck.gateway.backend import FakeBackend
from tests.support.gateway.cookie_socket import CookieSocket, until


def controls(session: Any) -> dict[str, Any]:
    result: dict[str, Any] = {}

    def walk(obj: Any) -> None:
        if isinstance(obj, list | tuple):
            for child in obj:
                walk(child)
        elif obj is not None:
            if getattr(obj, "key", None):
                result[obj.key] = obj
            for attr in ("_b", "controls", "content"):
                walk(getattr(obj, attr, None))

    walk(session.page.views[0].controls)
    return result


@pytest.mark.parametrize("active", [False, True])
@pytest.mark.parametrize("next_ip", ["192.0.2.10", "198.51.100.20"])
def test_network_change_preserves_actual_session_and_editor_without_rerunning_main(
    active: bool,
    next_ip: str,
) -> None:
    async def scenario() -> None:
        state = GatewayState(backend=FakeBackend(), cursor_secret=b"x" * 32)
        store = WebSessionStore("http://test", b"s" * 32)
        sid = store.create_session("synthetic", "device", "access", "refresh", 2_000_000)
        created: list[str] = []
        resumed: list[str] = []
        mutations: list[str] = []

        def main(page: ft.Page) -> None:
            created.append(page.session.id)

            @ft.component
            def Screen() -> Any:
                draft, set_draft = ft.use_state("initial")
                sheet, set_sheet = ft.use_state(False)

                async def edit(e: Any) -> None:
                    set_draft(e.data)

                return ft.Column(
                    controls=[
                        ft.TextField(key="draft", value=draft, on_change=edit),
                        ft.Button("Sheet", key="open", on_click=lambda e: set_sheet(True)),
                        ft.Container(key="sheet", visible=sheet, content=ft.Text("fields")),
                        ft.Button("Send", key="send", on_click=lambda e: mutations.append("send")),
                    ]
                )

            async def reconnect(e: Any) -> None:
                await rebind_on_connect(store, page)
                resumed.append(page.session.id)

            page.on_connect = reconnect
            page.render(Screen)

        app, _ = build_web_app(
            gateway_state=state,
            gateway_app=create_app(state),
            flet_main=main,
            public_origin=store.public_origin,
            cookie_secret=store.cookie_secret,
            store=store,
        )
        cookie = f"{COOKIE_NAME}={store.sign_sid(sid)}"
        sockets: list[CookieSocket] = []
        async with app.router.lifespan_context(app):
            try:
                old = await CookieSocket(app, cookie).open()
                sockets.append(old)
                fid = await old.register()
                assert isinstance(fid, str)
                unique = store.unique_key(fid)
                session = await app_manager.get_session(unique)
                await until(lambda: "draft" in controls(session))
                retained = controls(session)
                draft = "exact unsent\nmultiline draft"
                await old.packet(4, {"id": retained["draft"]._i, "props": {"value": draft}})
                await old.packet(
                    3, {"target": retained["draft"]._i, "name": "change", "data": draft}
                )
                await old.packet(3, {"target": retained["open"]._i, "name": "click", "data": ""})
                await until(lambda: controls(session)["sheet"].visible)
                retained = controls(session)
                if not active:
                    await old.close()
                    assert session.connection is None
                replacement = await CookieSocket(app, cookie, next_ip).open()
                sockets.append(replacement)
                assert await replacement.register(fid) == fid
                await until(lambda: resumed == [fid])
                assert created == [fid]
                assert await app_manager.get_session(unique) is session
                assert store.unique_key(fid) == unique
                assert session.page.route == "/agents/synthetic:term"
                assert session.connection is not None and session.expires_at is None
                current = controls(session)
                assert current["draft"].value == draft and current["sheet"].visible
                assert current == retained
                assert all(current[key] is retained[key] for key in retained)
                assert mutations == []
                if active:
                    assert old.task is not None
                    await asyncio.wait_for(asyncio.shield(old.task), 2)
                    # Late traffic/close cannot mutate or disconnect the successor.
                    await old.packet(
                        3, {"target": retained["send"]._i, "name": "click", "data": None}
                    )
                    await old.close()
                    assert session.connection is not None and mutations == []
                await teardown_web_sid(store, state, sid)
                assert await app_manager.get_session(unique) is None
                assert replacement.task is not None
                await asyncio.wait_for(asyncio.shield(replacement.task), 2)
            finally:
                for socket in sockets:
                    await socket.close()

    asyncio.run(scenario())


@pytest.mark.parametrize("timeout_env", [None, "", "37"])
@pytest.mark.parametrize("sender_failure", [False, True])
def test_reconnect_detaches_failed_sender_once_and_preserves_session(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    timeout_env: str | None,
    sender_failure: bool,
) -> None:
    from flet_web.fastapi import flet_app  # type: ignore[import-untyped]

    import relaydeck.client.serve as serve

    if timeout_env is None:
        monkeypatch.delenv("FLET_SESSION_TIMEOUT", raising=False)
    else:
        monkeypatch.setenv("FLET_SESSION_TIMEOUT", timeout_env)
    timeout = int(timeout_env) if timeout_env else flet_app.DEFAULT_FLET_SESSION_TIMEOUT
    caplog.set_level(logging.INFO, logger=serve.__name__)

    async def scenario() -> None:
        state = GatewayState(backend=FakeBackend(), cursor_secret=b"x" * 32)
        store = WebSessionStore("http://test", b"s" * 32)
        sid = store.create_session("synthetic", "device", "access", "refresh", 2_000_000)
        pages: list[ft.Page] = []
        disconnected: list[str] = []
        resumed: list[str] = []
        transports: list[serve._FletTransport] = []
        disconnects: list[tuple[str, int]] = []
        disconnect_session = app_manager.disconnect_session

        async def track_disconnect(key: str, seconds: int) -> None:
            disconnects.append((key, seconds))
            await disconnect_session(key, seconds)

        monkeypatch.setattr(app_manager, "disconnect_session", track_disconnect)

        def main(page: ft.Page) -> None:
            pages.append(page)
            transport = serve._current_transport.get()
            assert transport is not None
            transports.append(transport)

            async def on_disconnect(e: Any) -> None:
                disconnected.append(page.session.id)

            async def on_connect(e: Any) -> None:
                await rebind_on_connect(store, page)
                resumed.append(page.session.id)

            page.on_disconnect = on_disconnect
            page.on_connect = on_connect
            page.add(ft.TextField(key="draft", value="exact unsent\nretained draft"))

        app, _ = build_web_app(
            gateway_state=state,
            gateway_app=create_app(state),
            flet_main=main,
            public_origin=store.public_origin,
            cookie_secret=store.cookie_secret,
            store=store,
        )
        armed, failed = asyncio.Event(), asyncio.Event()
        sender_error = WebSocketDisconnect(1006)
        contexts_after: list[tuple[Any, Any, Any]] = []

        async def failing_carrier(scope: Any, receive: Any, send: Any) -> None:
            async def outbound(message: Any) -> None:
                if armed.is_set() and message["type"] == "websocket.send":
                    failed.set()
                    raise sender_error
                await send(message)

            try:
                await app(scope, receive, outbound)
            finally:
                contexts_after.append(
                    (
                        serve._current_handshake.get(),
                        serve._current_browser.get(),
                        serve._current_transport.get(),
                    )
                )

        cookie = f"{COOKIE_NAME}={store.sign_sid(sid)}"
        sockets: list[CookieSocket] = []
        async with app.router.lifespan_context(app):
            try:
                old = await CookieSocket(failing_carrier, cookie).open()
                sockets.append(old)
                fid = await old.register()
                assert isinstance(fid, str)
                unique = store.unique_key(fid)
                assert unique is not None
                session = await app_manager.get_session(unique)
                assert session is not None
                await until(lambda: "draft" in controls(session))
                retained = controls(session)["draft"]
                before_close = datetime.now(UTC)
                if sender_failure:
                    armed.set()
                    pages[0].title = "force one outbound update"
                    pages[0].update()
                    await asyncio.wait_for(failed.wait(), 2)
                    await old.inbound.put({"type": "websocket.disconnect", "code": 1001})
                    assert old.task is not None
                    with pytest.raises(WebSocketDisconnect) as raised:
                        await asyncio.wait_for(asyncio.shield(old.task), 2)
                    assert raised.value is sender_error
                else:
                    await old.close()
                after_close = datetime.now(UTC)
                assert session.connection is None
                assert session.expires_at is not None
                assert before_close + timedelta(seconds=timeout) <= session.expires_at
                assert session.expires_at <= after_close + timedelta(seconds=timeout)
                assert disconnects == [(unique, timeout)] and disconnected == [fid]
                assert transports[0].closed.is_set() and transports[0].done.is_set()
                assert contexts_after == [(None, None, None)] and store._closers == {}
                detached = [
                    record.getMessage()
                    for record in caplog.records
                    if record.getMessage().startswith("flet_stale_connection_detached")
                ]
                assert detached == (
                    [f"flet_stale_connection_detached flet={fid[:8]}"] if sender_failure else []
                )
                replacement = await CookieSocket(app, cookie, "198.51.100.20").open()
                sockets.append(replacement)
                assert await replacement.register(fid) == fid
                await until(lambda: resumed == [fid])
                assert pages == [session.page]
                assert await app_manager.get_session(unique) is session
                assert session.connection is not None and session.expires_at is None
                assert controls(session)["draft"] is retained
                assert retained.value == "exact unsent\nretained draft"
                assert disconnected == [fid]
            finally:
                for socket in sockets:
                    if socket.task is not None and not socket.task.done():
                        await socket.close()

    asyncio.run(scenario())


@pytest.mark.parametrize("replacement", ["before-lookup", "during-lookup", "removed"])
def test_reconnect_stale_cleanup_never_detaches_a_successor(
    monkeypatch: pytest.MonkeyPatch, replacement: str
) -> None:
    from types import SimpleNamespace

    import relaydeck.client.serve as serve

    async def scenario() -> None:
        store = WebSessionStore("http://test", b"s" * 32)
        sid = store.create_session("synthetic", "device", "access", "refresh", 2_000_000)
        fid = "owned-page-successor"
        captured: list[serve._FletTransport] = []
        lookups: list[str] = []
        disconnects: list[str] = []
        candidate = SimpleNamespace(connection=object())

        async def close_successor() -> None:
            pytest.fail("old cleanup must not close the successor")

        successor = serve._FletTransport(close_successor, asyncio.Event(), flet_id=fid)

        async def lookup(key: str) -> Any:
            lookups.append(key)
            # Exercise the ownership recheck after an actually suspended lookup.
            await asyncio.sleep(0)
            assert captured[0].closed.is_set() and not captured[0].done.is_set()
            guard._transports[fid] = successor
            return candidate

        async def disconnect(key: str, seconds: int) -> None:
            disconnects.append(key)
            candidate.connection = None

        monkeypatch.setattr(app_manager, "get_session", lookup)
        monkeypatch.setattr(app_manager, "disconnect_session", disconnect)

        async def downstream(scope: Any, receive: Any, send: Any) -> None:
            await receive()
            await receive()
            transport = serve._current_transport.get()
            assert transport is not None
            captured.append(transport)
            store.bind_flet(sid, fid, serve._Handshake("192.0.2.10", "synthetic", ""))
            await send(
                {
                    "type": "websocket.send",
                    "bytes": b"\x00" + msgpack.packb([1, {"session_id": fid}]),
                }
            )
            assert guard._transports[fid] is transport
            if replacement == "before-lookup":
                guard._transports[fid] = successor
            elif replacement == "removed":
                del guard._transports[fid]

        guard = FletGuardMiddleware(downstream, store)
        socket = await CookieSocket(guard, f"{COOKIE_NAME}={store.sign_sid(sid)}").open()
        assert await socket.register() == fid
        assert socket.task is not None
        await asyncio.wait_for(socket.task, 2)
        assert lookups == ([store.unique_key(fid)] if replacement == "during-lookup" else [])
        assert disconnects == [] and candidate.connection is not None
        assert guard._transports == ({} if replacement == "removed" else {fid: successor})
        assert not successor.closed.is_set() and not successor.done.is_set()
        assert captured[0].done.is_set() and store._closers == {} and guard._handoffs == {}

    asyncio.run(scenario())


@pytest.mark.parametrize("failure_at", ["lookup", "disconnect"])
def test_reconnect_stale_cleanup_exception_keeps_original_failure_and_finalizes(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture, failure_at: str
) -> None:
    from types import SimpleNamespace

    import relaydeck.client.serve as serve

    async def scenario() -> None:
        store = WebSessionStore("http://test", b"s" * 32)
        sid = store.create_session("synthetic", "device", "access", "refresh", 2_000_000)
        fid = "owned-page-cleanup-error"
        captured: list[serve._FletTransport] = []
        contexts_after: list[tuple[Any, Any, Any]] = []
        cleanup_calls: list[str] = []
        original_error, cleanup_error = RuntimeError("downstream failed"), OSError("cleanup failed")

        async def lookup(key: str) -> Any:
            cleanup_calls.append("lookup")
            assert captured[0].closed.is_set() and not captured[0].done.is_set()
            assert guard._transports[fid] is captured[0]
            assert serve._current_transport.get() is captured[0]
            if failure_at == "lookup":
                raise cleanup_error
            return SimpleNamespace(connection=object())

        async def disconnect(key: str, seconds: int) -> None:
            cleanup_calls.append("disconnect")
            raise cleanup_error

        monkeypatch.setattr(app_manager, "get_session", lookup)
        monkeypatch.setattr(app_manager, "disconnect_session", disconnect)

        async def downstream(scope: Any, receive: Any, send: Any) -> None:
            await receive()
            await receive()
            transport = serve._current_transport.get()
            assert transport is not None
            captured.append(transport)
            store.bind_flet(sid, fid, serve._Handshake("192.0.2.10", "synthetic", ""))
            await send(
                {
                    "type": "websocket.send",
                    "bytes": b"\x00" + msgpack.packb([1, {"session_id": fid}]),
                }
            )
            raise original_error

        guard = FletGuardMiddleware(downstream, store)

        async def carrier(scope: Any, receive: Any, send: Any) -> None:
            try:
                await guard(scope, receive, send)
            finally:
                contexts_after.append(
                    (
                        serve._current_handshake.get(),
                        serve._current_browser.get(),
                        serve._current_transport.get(),
                    )
                )

        socket = await CookieSocket(carrier, f"{COOKIE_NAME}={store.sign_sid(sid)}").open()
        assert await socket.register() == fid
        assert socket.task is not None
        with pytest.raises(RuntimeError) as raised:
            await asyncio.wait_for(socket.task, 2)
        assert raised.value is original_error
        assert cleanup_calls == (["lookup"] if failure_at == "lookup" else ["lookup", "disconnect"])
        assert captured[0].closed.is_set() and captured[0].done.is_set()
        assert guard._transports == {} and guard._handoffs == {} and store._closers == {}
        assert contexts_after == [(None, None, None)]
        warnings = [
            record for record in caplog.records if record.name == serve.__name__ and record.exc_info
        ]
        assert len(warnings) == 1 and warnings[0].levelno == logging.WARNING
        assert warnings[0].exc_info is not None and warnings[0].exc_info[1] is cleanup_error
        assert not any(
            record.getMessage().startswith("flet_stale_connection_detached")
            for record in caplog.records
        )

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "race", ["duplicate", "logout", "expiry", "owner-change", "cancel", "timeout"]
)
def test_handoff_waits_for_entire_old_handler_and_revalidates_identity(
    monkeypatch: Any, race: str
) -> None:
    async def scenario() -> None:
        import relaydeck.client.serve as serve

        draining, allow_cleanup = asyncio.Event(), asyncio.Event()
        guards: list[FletGuardMiddleware] = []
        original_init = FletGuardMiddleware.__init__

        def delayed_init(self: Any, app: Any, store: Any, ws_path: str = "/ws") -> None:
            count = 0

            async def downstream(scope: Any, receive: Any, send: Any) -> None:
                nonlocal count
                count += 1
                first = count == 1
                try:
                    await app(scope, receive, send)
                finally:
                    if first:
                        draining.set()
                        await allow_cleanup.wait()

            original_init(self, downstream, store, ws_path)
            guards.append(self)

        monkeypatch.setattr(FletGuardMiddleware, "__init__", delayed_init)
        if race == "timeout":
            monkeypatch.setattr(serve, "FLET_HANDOFF_TIMEOUT_S", 0.05)
        state = GatewayState(backend=FakeBackend(), cursor_secret=b"x" * 32)
        store = WebSessionStore("http://test", b"s" * 32)
        sid = store.create_session("synthetic", "device", "access", "refresh", 2_000_000)
        created: list[str] = []
        connected: list[str] = []

        def main(page: ft.Page) -> None:
            created.append(page.session.id)
            page.add(ft.Text("retained"))

            async def on_connect(e: Any) -> None:
                await rebind_on_connect(store, page)
                connected.append(page.session.id)

            page.on_connect = on_connect

        app, _ = build_web_app(
            gateway_state=state,
            gateway_app=create_app(state),
            flet_main=main,
            public_origin=store.public_origin,
            cookie_secret=store.cookie_secret,
            store=store,
        )
        cookie = f"{COOKIE_NAME}={store.sign_sid(sid)}"
        sockets: list[CookieSocket] = []
        async with app.router.lifespan_context(app):
            try:
                old = await CookieSocket(app, cookie).open()
                sockets.append(old)
                fid = await old.register()
                assert isinstance(fid, str)
                unique = store.unique_key(fid)
                session = await app_manager.get_session(unique)
                await until(lambda: created == [fid])
                replacement = await CookieSocket(app, cookie, "198.51.100.20").open()
                sockets.append(replacement)
                registration = asyncio.create_task(replacement.register(fid))
                await asyncio.wait_for(draining.wait(), 2)
                assert not registration.done() and session.connection is None
                guard = guards[0]
                assert len(guard._handoffs) == 1
                if race == "duplicate":
                    duplicate = await CookieSocket(app, cookie, "203.0.113.30").open()
                    sockets.append(duplicate)
                    denial = await duplicate.register(fid)
                    assert (
                        isinstance(denial, dict) and denial["reason"] == "session_recovery_required"
                    )
                    assert len(guard._handoffs) == 1 and not registration.done()
                elif race == "logout":
                    await teardown_web_sid(store, state, sid)
                elif race == "expiry":
                    store._sessions[sid].expires_ms = store.now_ms() - 1
                elif race == "owner-change":
                    other = store.create_session("other", "other", "oa", "or", 2_000_000)
                    store.bind_flet(other, fid, store._handshakes[fid])
                elif race == "cancel":
                    assert replacement.task is not None
                    replacement.task.cancel()
                    with pytest.raises(asyncio.CancelledError):
                        await replacement.task
                    registration.cancel()
                    with pytest.raises(asyncio.CancelledError):
                        await registration
                    assert guard._handoffs == {}
                if race == "timeout":
                    denial = await registration
                    assert (
                        isinstance(denial, dict) and denial["reason"] == "session_recovery_required"
                    )
                    assert session.connection is None and guard._handoffs == {}
                allow_cleanup.set()
                assert old.task is not None
                await asyncio.wait_for(asyncio.shield(old.task), 2)
                if race == "duplicate":
                    assert await registration == fid
                    await until(lambda: connected == [fid])
                    assert await app_manager.get_session(unique) is session
                    assert session.connection is not None and session.expires_at is None
                elif race in ("logout", "expiry", "owner-change"):
                    denial = await registration
                    assert isinstance(denial, dict) and denial["code"] == 1008
                    assert denial["reason"] == (
                        "logout" if race == "logout" else "session_recovery_required"
                    )
                    assert connected == []
                elif race in ("cancel", "timeout"):
                    # A failed claimant neither leaves a lock nor captures a future successor.
                    next_socket = await CookieSocket(app, cookie, "203.0.113.40").open()
                    sockets.append(next_socket)
                    assert await next_socket.register(fid) == fid
                    await until(lambda: connected == [fid])
                    assert session.connection is not None
                assert created == [fid] and guard._handoffs == {}
            finally:
                allow_cleanup.set()
                # Cancelled request tasks are deliberately covered above.
                for socket in sockets:
                    if socket.task is not None and socket.task.cancelled():
                        continue
                    await socket.close()
                unique = store.unique_key(fid) if isinstance(fid, str) else None
                if unique is not None:
                    await app_manager.delete_session(unique)
                assert guards[0]._transports == {} and guards[0]._handoffs == {}
                assert store._closers == {}

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "case",
    [
        "missing-cookie",
        "tampered-cookie",
        "foreign-account",
        "other-device",
        "new-lineage",
        "expired",
        "revoked",
        "changed-ua",
        "changed-page",
        "bad-origin",
        "duplicate-origin",
        "anonymous-browser-change",
        "anonymous",
        "unknown",
        "same-owner",
        "untracked-active",
        "missing-candidate",
    ],
)
def test_canonical_peer_is_only_for_valid_owned_page_with_identical_identity(
    monkeypatch: Any, case: str
) -> None:
    from types import SimpleNamespace

    from relaydeck.client.serve import _Handshake

    async def scenario() -> None:
        store = WebSessionStore("http://test", b"s" * 32)
        sid = store.create_session("owner", "device", "access", "refresh", 2_000_000)
        canonical = _Handshake("192.0.2.10", "synthetic", "")
        store.bind_flet(sid, "owned-page", canonical)
        cookie = f"{COOKIE_NAME}={store.sign_sid(sid)}"
        fid, ua, path = "owned-page", "synthetic", "/ws"
        if case == "missing-cookie":
            cookie = ""
        elif case == "tampered-cookie":
            cookie += "tampered"
        elif case in ("foreign-account", "other-device", "new-lineage"):
            other = store.create_session(
                "foreign" if case == "foreign-account" else "owner",
                "other" if case == "other-device" else "device",
                "other-access",
                "other-refresh",
                2_000_000,
            )
            cookie = f"{COOKIE_NAME}={store.sign_sid(other)}"
        elif case == "expired":
            store._sessions[sid].expires_ms = store.now_ms() - 1
        elif case == "revoked":
            store._sessions.pop(sid)
        elif case == "changed-ua":
            ua = "other-agent"
        elif case == "changed-page":
            path = "/nested/ws"
        elif case in ("anonymous", "anonymous-browser-change"):
            cookie = ""
            store.bind_flet(None, fid, canonical)
            if case == "anonymous-browser-change":
                store.browser_pages[fid] = "another-browser"
        elif case == "unknown":
            fid = "unknown-page"
        headers = [
            (b"origin", b"http://other" if case == "bad-origin" else b"http://test"),
            (b"user-agent", ua.encode()),
            (b"cookie", cookie.encode()),
        ]
        if case == "duplicate-origin":
            headers.append((b"origin", b"http://test"))
        scope = {
            "type": "websocket",
            "path": path,
            "headers": headers,
            "client": ("198.51.100.20", 1234),
        }
        lookup: list[str] = []

        async def candidate(key: str) -> Any:
            lookup.append(key)
            if case == "missing-candidate":
                return None
            return SimpleNamespace(connection=object() if case == "untracked-active" else None)

        monkeypatch.setattr(app_manager, "get_session", candidate)
        frame = b"\x00" + msgpack.packb([1, {"session_id": fid, "page_name": "", "page": {}}])
        incoming = iter(
            [
                {"type": "websocket.connect"},
                {"type": "websocket.receive", "bytes": frame},
            ]
        )
        outgoing: list[Any] = []
        scopes: list[Any] = []

        async def receive() -> Any:
            return next(incoming)

        async def send(message: Any) -> None:
            outgoing.append(message)

        async def downstream(held: Any, receive: Any, send: Any) -> None:
            scopes.append(held)
            assert (await receive())["type"] == "websocket.connect"
            assert await receive() == {"type": "websocket.receive", "bytes": frame}

        guard = FletGuardMiddleware(downstream, store, ws_path=path)
        await guard(scope, receive, send)
        if case == "same-owner":
            assert scopes[0]["client"] == ("192.0.2.10", 1234)
            assert scopes[0]["relaydeck.actual_client"] == scope["client"]
            assert lookup == [store.unique_key(fid)]
        elif case in ("anonymous", "unknown"):
            assert scopes == [scope] and lookup == []
            assert "relaydeck.actual_client" not in scopes[0]
        else:
            assert scopes == []
            assert outgoing[-1]["type"] == "websocket.close" and outgoing[-1]["code"] == 1008
            assert lookup == (
                [store.unique_key(fid)] if case in ("untracked-active", "missing-candidate") else []
            )
        assert scope["client"] == ("198.51.100.20", 1234)
        assert store._handshakes[fid if case != "unknown" else "owned-page"] == canonical
        assert store._closers == {} and guard._handoffs == {} and guard._transports == {}

    asyncio.run(scenario())
