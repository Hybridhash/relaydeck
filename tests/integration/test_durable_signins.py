"""Durable authority plus real cookie/Flet boundary; synthetic private storage."""

from __future__ import annotations

import asyncio
import shutil
import sqlite3
from pathlib import Path
from typing import Any

import flet as ft
import httpx
import pytest

from relaydeck.client.serve import COOKIE_NAME, WebSessionStore, build_web_app
from relaydeck.companion.persistence import open_store
from relaydeck.gateway.app import GatewayState, create_app
from relaydeck.gateway.backend import FakeBackend
from relaydeck.gateway.identity import SessionStore
from relaydeck.persistence_contracts import StorageUnavailable, Table
from tests.support.gateway.cookie_socket import CookieSocket, until
from tests.support.persistence.persistence import Config, MemoryKeys

ORIGIN = "http://test"


def stores(
    path: Path, keys: MemoryKeys, clock: list[int]
) -> tuple[Any, SessionStore, WebSessionStore]:
    disk = open_store(Config(path), keys)
    sessions = SessionStore(
        persistence=disk,
        origin=ORIGIN,
        generation_reader=lambda: keys.restore_generation("authentication").value,
    )
    web = WebSessionStore(
        ORIGIN,
        b"cookie-canary" * 4,
        now_ms=lambda: clock[0],
        persistence=disk,
        gateway_sessions=sessions,
    )
    return disk, sessions, web


def seed(path: Path) -> tuple[MemoryKeys, list[int], Any, SessionStore, WebSessionStore, str]:
    keys, clock = MemoryKeys(), [1_900_000_000_000]
    keys.restore_generation("authentication", create=True)
    disk, sessions, web = stores(path, keys, clock)
    access, refresh = sessions.create("account", ["sessions.read"], ["host"], clock[0], "device")
    sid = web.create_session("account", "device", access, refresh, clock[0] + 900_000)
    return keys, clock, disk, sessions, web, sid


def test_restart_same_tab_no_pairing(tmp_path: Path) -> None:
    keys, clock, disk, sessions, web, sid = seed(tmp_path / "state")
    cookie = web.sign_sid(sid)
    saved_expiry = web._sessions[sid].expires_ms
    created: list[tuple[str, str]] = []

    async def page(store: WebSessionStore, authority: SessionStore, requested: str = "") -> str:
        state = GatewayState(FakeBackend(), sessions=authority, now_ms=lambda: clock[0])

        def main(p: ft.Page) -> None:
            created.append((p.session.id, store.draft_tab_for(sid, p.session.id)))
            p.add(ft.Text("synthetic authenticated page"))

        app, _ = build_web_app(
            gateway_state=state,
            gateway_app=create_app(state),
            flet_main=main,
            public_origin=ORIGIN,
            cookie_secret=store.cookie_secret,
            store=store,
        )
        async with app.router.lifespan_context(app):
            socket = await CookieSocket(app, f"{COOKIE_NAME}={cookie}").open()
            fid = ""
            try:
                registered = await socket.register(requested)
                assert isinstance(registered, str)
                fid = registered
                await until(lambda: any(item[0] == fid for item in created))
                assert store.identity(sid).device_id == "device"  # type: ignore[union-attr]
                assert isinstance(state.backend, FakeBackend)
                assert not state.backend.ownership._leases
                async with httpx.AsyncClient(
                    transport=httpx.ASGITransport(app=app), base_url=ORIGIN
                ) as http:
                    access = store.take_session(sid)[0]  # type: ignore[index]
                    assert (
                        await http.get("/v1/me", headers={"authorization": f"Bearer {access}"})
                    ).status_code == 200
                return fid
            finally:
                await socket.close()
                # Flet's shutdown keeps its Python singleton; actual process
                # restart does not. Remove this page through the supported API.
                from flet.fastapi import app_manager

                unique = store.unique_key(fid)
                if unique is not None:
                    await app_manager.delete_session(unique)

    first = asyncio.run(page(web, sessions))
    disk.close()
    clock[0] += 1000
    disk, sessions, restored = stores(tmp_path / "state", keys, clock)
    try:
        assert not restored._owners and not restored._sessions
        second = asyncio.run(page(restored, sessions, first))
        assert second != first and created[0][1] == created[1][1]
        assert restored._sessions[sid].expires_ms == saved_expiry
        # The raw sid and bearer pair are encrypted, never serialized as indexes.
        content = b"".join(p.read_bytes() for p in (tmp_path / "state").glob("persistence.db*"))
        assert sid.encode() not in content
        assert all(token.encode() not in content for token in restored.take_session(sid) or ())
    finally:
        disk.close()


def test_refresh_crash_atomic_and_parallel(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    keys, clock, disk, sessions, web, sid = seed(tmp_path / "state")
    old = web.take_session(sid)
    clock[0] += 900_000
    original = disk.transaction
    from contextlib import contextmanager

    @contextmanager
    def interrupted() -> Any:
        with original() as tx:
            yield tx
            row = tx.get(Table.WEB_SESSIONS, web._sid_key(sid))
            if row is not None and row.revision > 1:
                raise StorageUnavailable("full")

    async def forbidden_refresher(token: str) -> Any:
        pytest.fail("durable web refresh must use the single local transaction")

    monkeypatch.setattr(disk, "transaction", interrupted)
    with pytest.raises(StorageUnavailable):
        asyncio.run(web.ensure_access(sid, forbidden_refresher, force=True))
    assert web.take_session(sid) == old
    monkeypatch.setattr(disk, "transaction", original)
    disk.close()
    disk, sessions, web = stores(tmp_path / "state", keys, clock)
    try:
        assert web.verify_cookie(web.sign_sid(sid)) == sid
        assert web.take_session(sid) == old

        async def parallel() -> list[str | None]:
            return await asyncio.gather(
                *(web.ensure_access(sid, forbidden_refresher, force=True) for _ in range(8))
            )

        results = asyncio.run(parallel())
        assert len(set(results)) == 1 and results[0] != old[0]  # type: ignore[index]
        assert sessions.refresh(old[1], clock[0]) is None  # type: ignore[index]
        pair = web.take_session(sid)
        disk.close()
        disk, sessions, web = stores(tmp_path / "state", keys, clock)
        assert web.verify_cookie(web.sign_sid(sid)) == sid and web.take_session(sid) == pair
    finally:
        disk.close()


def test_revocation_survives_restart_and_backup_restore(tmp_path: Path) -> None:
    keys, clock, disk, sessions, web, sid = seed(tmp_path / "state")
    pair = web.take_session(sid)
    cookie = web.sign_sid(sid)
    backup = tmp_path / "backup.db"
    with sqlite3.connect(backup) as target:
        disk._db.backup(target)
    assert sessions.revoke_device("account", "device") == 1
    assert web.verify_cookie(cookie) is None
    disk.close()
    disk, sessions, web = stores(tmp_path / "state", keys, clock)
    assert web.verify_cookie(cookie) is None and sessions.refresh(pair[1], clock[0]) is None  # type: ignore[index]
    disk.close()
    # Explicit recovery MUST advance the Keychain-held generation before restore.
    keys.advance_restore_generation("authentication")
    shutil.copyfile(backup, tmp_path / "state/persistence.db")
    disk, sessions, web = stores(tmp_path / "state", keys, clock)
    try:
        assert web.verify_cookie(cookie) is None
        assert sessions.lookup(pair[0], clock[0]) is None  # type: ignore[index]
        assert sessions.refresh(pair[1], clock[0]) is None  # type: ignore[index]
        with disk.transaction() as tx:
            assert tx.scan(Table.WEB_SESSIONS, scope=(ORIGIN, "account", "device")).records
    finally:
        disk.close()


def test_foreign_expired_origin_and_duplicate_tab(tmp_path: Path) -> None:
    keys, clock, disk, sessions, web, sid = seed(tmp_path / "state")
    try:
        cookie = web.sign_sid(sid)
        original = web.claim_draft_tab(sid, "", "first")
        restored = web.claim_draft_tab(sid, "first", "next")
        assert restored != original  # live duplicate forks only its draft namespace
        assert web.claim_draft_tab(sid, "first", "next") == restored
        other = WebSessionStore(
            "https://foreign.test", web.cookie_secret, persistence=disk, gateway_sessions=sessions
        )
        assert other.verify_cookie(cookie) is None
        assert web.verify_cookie(cookie + "broken") is None
        clock[0] += 30 * 24 * 3600 * 1000 + 1  # 30 days without use (sliding lifetime)
        assert web.verify_cookie(cookie) is None
        assert not web._pending and not web.browsers  # pairing state is never restored
    finally:
        disk.close()


def test_explicit_signer_migration_is_idempotent_and_lock_never_reads_plaintext(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from relaydeck.companion.config import CompanionConfig
    from relaydeck.companion.runtime import CompanionRuntime

    cookie, cursor = tmp_path / "cookie.secret", tmp_path / "cursor.secret"
    cookie.write_text("k" * 48 + "\n")
    cursor.write_text("c" * 32)
    cookie.chmod(0o600)
    cursor.chmod(0o600)
    config = CompanionConfig(
        ORIGIN, cookie, cursor, tmp_path / "state", tmp_path / "connector", "unused"
    )
    keys = MemoryKeys()
    for _ in range(2):
        runtime = CompanionRuntime(
            config,
            durable_signins=True,
            migrate_cookie_secret=True,
            setup_persistence=True,
            persistence_provider=keys,
        )
        signer, _ = runtime._storage_secrets()
        assert signer == b"k" * 48 and runtime.persistence_status.available
        assert cookie.read_bytes() == b"k" * 48 + b"\n"
        runtime.persistence.close()
    assert keys.read_secret("cookie-signing").value == b"k" * 48
    keys.locked = True
    monkeypatch.setattr(
        CompanionConfig, "secrets", lambda self: pytest.fail("no plaintext signer fallback")
    )
    locked = CompanionRuntime(config, durable_signins=True, persistence_provider=keys)
    signer, _ = locked._storage_secrets()
    assert signer != b"k" * 48 and locked.persistence is None
    assert locked.persistence_status.reason == "keys_unavailable"
    assert cookie.exists()


@pytest.mark.parametrize("mode", ["session", "device", "account", "host"])
def test_durable_revoke_scope_invalidates_web_before_ack_and_preserves_foreign(
    tmp_path: Path,
    mode: str,
) -> None:
    keys, clock, disk, sessions, web, sid = seed(tmp_path / "state")
    first = web.take_session(sid)
    foreign = sessions.create("foreign", ["sessions.read"], ["host"], clock[0], "other")
    other_sid = web.create_session("foreign", "other", *foreign, clock[0] + 900_000)
    if mode == "host":
        paired = sessions.create(
            "account", ["sessions.read"], ["host"], clock[0], "device", host_binding="bound"
        )
        sid = web.create_session("account", "device", *paired, clock[0] + 900_000)
        first = paired
    cookie, foreign_cookie = web.sign_sid(sid), web.sign_sid(other_sid)
    if mode == "session":
        assert sessions.revoke_session(first[0]) is not None  # type: ignore[index]
    elif mode == "device":
        assert sessions.revoke_device("account", "device") == 1
    elif mode == "account":
        assert sessions.revoke_account("account") == 1
    else:
        assert len(sessions.revoke_paired_host("host")) == 1
    assert web.verify_cookie(cookie) is None
    disk.close()
    disk, sessions, web = stores(tmp_path / "state", keys, clock)
    try:
        assert web.verify_cookie(cookie) is None
        assert sessions.lookup(first[0], clock[0]) is None  # type: ignore[index]
        assert web.verify_cookie(foreign_cookie) == other_sid
        assert sessions.lookup(foreign[0], clock[0]) is not None
    finally:
        disk.close()


def test_revocation_commit_failure_denies_access_without_success_response(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from contextlib import contextmanager

    keys, clock, disk, sessions, web, sid = seed(tmp_path / "state")
    pair = web.take_session(sid)
    original = disk.transaction

    @contextmanager
    def interrupted() -> Any:
        with original() as tx:
            yield tx
            if tx.scan(Table.REVOCATIONS, scope=(ORIGIN, "account")).records:
                raise StorageUnavailable("full")

    state = GatewayState(FakeBackend(), sessions=sessions, now_ms=lambda: clock[0])
    app = create_app(state)
    monkeypatch.setattr(disk, "transaction", interrupted)

    async def logout() -> None:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url=ORIGIN
        ) as http:
            response = await http.post("/v1/logout", headers={"authorization": f"Bearer {pair[0]}"})  # type: ignore[index]
            assert (
                response.status_code == 503
                and response.json()["details"]["storage_reason"] == "full"
            )

    try:
        asyncio.run(logout())
        monkeypatch.setattr(disk, "transaction", original)
        assert sessions.lookup(pair[0], clock[0]) is None  # type: ignore[index]
        assert web.verify_cookie(web.sign_sid(sid)) is None
    finally:
        disk.close()


def test_current_grants_and_locked_keychain_are_rechecked(tmp_path: Path) -> None:
    keys, clock, disk, sessions, web, sid = seed(tmp_path / "state")
    cookie = web.sign_sid(sid)
    try:
        assert web.verify_cookie(cookie) == sid
        web.session_allowed = lambda session: False
        assert web.verify_cookie(cookie) is None
        web.session_allowed = lambda session: True
        assert web.verify_cookie(cookie) == sid
        keys.locked = True
        assert web.verify_cookie(cookie) is None
        assert not disk.status().available
    finally:
        keys.locked = False
        disk.close()


def test_locked_durable_storage_still_starts_companion_without_listener(tmp_path: Path) -> None:
    from relaydeck.companion.config import CompanionConfig
    from relaydeck.companion.runtime import CompanionRuntime

    cursor = tmp_path / "cursor.secret"
    cursor.write_text("c" * 32)
    cursor.chmod(0o600)
    config = CompanionConfig(
        "https://synthetic.test.ts.net",
        tmp_path / "absent-cookie",
        cursor,
        tmp_path / "state",
        tmp_path / "connector",
        "unused",
    )
    keys = MemoryKeys()
    keys.locked = True

    class Server:
        started = False
        should_exit = False

        async def serve(self) -> None:
            self.started = True
            while not self.should_exit:
                await asyncio.sleep(0.001)

    async def scenario() -> None:
        runtime = CompanionRuntime(
            config,
            durable_signins=True,
            setup_persistence=False,
            migrate_cookie_secret=False,
            invalidate_restored_signins=False,
            persistence_provider=keys,
            server_factory=lambda app: Server(),
        )
        try:
            await runtime.start(asyncio.Event())
            report = runtime.report()
            assert report["api_ready"] and report["web_ready"]
            assert not report["durable_signins"] and report["signin_storage"] == "keys_unavailable"
            assert runtime.gateway.sessions.persistence is None
            assert runtime.web_store is not None and runtime.web_store.persistence is None
        finally:
            await runtime.close()

    asyncio.run(scenario())


def test_native_refresh_wire_contract_and_foreign_origin_guard(tmp_path: Path) -> None:
    from relaydeck.contracts.schemas import validate

    keys, clock, disk, sessions, web, sid = seed(tmp_path / "state")
    pair = web.take_session(sid)
    assert pair is not None
    state = GatewayState(FakeBackend(), sessions=sessions, now_ms=lambda: clock[0])
    app, _ = build_web_app(
        gateway_state=state,
        gateway_app=create_app(state),
        flet_main=lambda page: None,
        public_origin=ORIGIN,
        cookie_secret=web.cookie_secret,
        store=web,
    )

    async def scenario() -> None:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url=ORIGIN
        ) as http:
            response = await http.post(
                "/v1/token", json={"grant": "refresh_token", "refresh_token": pair[1]}
            )
            assert response.status_code == 200
            validate("relaydeck/gateway/v1/token-response", response.json())
            assert sessions.refresh(pair[1], clock[0]) is None
            response = await http.post(
                "/v1/auth/logout",
                headers={
                    "origin": "http://foreign",
                    "cookie": f"{COOKIE_NAME}={web.sign_sid(sid)}",
                },
            )
            assert response.status_code == 403
        # Real guard, foreign Origin; no Flet registration/ownership is admitted.
        from relaydeck.client.serve import FletGuardMiddleware

        admitted: list[bool] = []

        async def downstream(scope: Any, receive: Any, send: Any) -> None:
            admitted.append(True)

        sent: list[Any] = []

        async def send(message: Any) -> None:
            sent.append(message)

        scope = {
            "type": "websocket",
            "path": "/ws",
            "headers": [
                (b"origin", b"http://foreign"),
                (b"cookie", f"{COOKIE_NAME}={web.sign_sid(sid)}".encode()),
            ],
        }
        await FletGuardMiddleware(downstream, web)(scope, lambda: None, send)
        assert not admitted and sent[0]["type"] == "websocket.close"

    try:
        asyncio.run(scenario())
    finally:
        disk.close()


@pytest.mark.parametrize("failure", ["locked", "commit"])
def test_web_logout_failure_retry_is_durable_and_severs_writers(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failure: str
) -> None:
    from contextlib import contextmanager

    keys, clock, disk, sessions, web, sid = seed(tmp_path / "state")
    cookie, pair = web.sign_sid(sid), web.take_session(sid)
    assert pair is not None
    foreign = sessions.create("foreign", ["sessions.read"], ["host"], clock[0], "other")
    foreign_sid = web.create_session("foreign", "other", *foreign, clock[0] + 900_000)
    backend = FakeBackend(now_ms=lambda: clock[0])
    attachment, _ = backend.create_attachment(
        "host", "terminal", "agent", "account", "device", takeover=True
    )
    lease = backend.acquire_control(attachment.attachment_id, "account", "device")
    state = GatewayState(backend, sessions=sessions, now_ms=lambda: clock[0])
    app, _ = build_web_app(
        gateway_state=state,
        gateway_app=create_app(state),
        flet_main=lambda page: None,
        public_origin=ORIGIN,
        cookie_secret=web.cookie_secret,
        store=web,
    )
    closed: list[bool] = []
    web.add_closer(sid, lambda: closed.append(True))
    original = disk.transaction

    @contextmanager
    def interrupted() -> Any:
        with original() as tx:
            yield tx
            if tx.scan(Table.REVOCATIONS, scope=(ORIGIN, "account")).records:
                raise StorageUnavailable("full")

    if failure == "locked":
        keys.locked = True
    else:
        monkeypatch.setattr(disk, "transaction", interrupted)

    async def request() -> None:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url=ORIGIN
        ) as http:
            headers = {"cookie": f"{COOKIE_NAME}={cookie}", "origin": ORIGIN}
            response = await http.post("/v1/auth/logout", headers=headers)
            assert response.status_code == 503
            assert (
                "set-cookie" not in response.headers
            )  # Keep the credential for revocation-only retry.
            assert closed == [True]
            assert web._pending_revocations[sid].identity.account == "account"
            assert not backend.ownership._leases[lease.lease_id].live
            assert sessions.pending_revocation(pair[0]) is not None
            keys.locked = False
            monkeypatch.setattr(disk, "transaction", original)
            assert web.verify_cookie(cookie) is None
            assert sessions.lookup(pair[0], clock[0]) is None
            assert sessions.refresh(pair[1], clock[0]) is None
            assert web.verify_cookie(web.sign_sid(foreign_sid)) == foreign_sid
            # A signature failure cannot select a pending or foreign revocation.
            bad = await http.post(
                "/v1/auth/logout", headers={**headers, "cookie": f"{COOKIE_NAME}={cookie}bad"}
            )
            assert bad.status_code == 204
            retry = await http.post("/v1/auth/logout", headers=headers)
            assert retry.status_code == 204
            assert sid not in web._pending_revocations
            assert sessions.pending_revocation(pair[0]) is None
            assert (await http.post("/v1/auth/logout", headers=headers)).status_code == 204

    try:
        asyncio.run(request())
    finally:
        keys.locked = False
        monkeypatch.setattr(disk, "transaction", original)
        disk.close()
    disk, restored_sessions, restored_web = stores(tmp_path / "state", keys, clock)
    try:
        assert restored_web.verify_cookie(cookie) is None
        assert restored_sessions.lookup(pair[0], clock[0]) is None
        assert restored_sessions.refresh(pair[1], clock[0]) is None
        assert restored_web.verify_cookie(web.sign_sid(foreign_sid)) == foreign_sid
    finally:
        disk.close()


@pytest.mark.parametrize("failure", ["locked", "commit"])
def test_native_logout_retry_bypasses_denial_only_to_revoke(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failure: str
) -> None:
    from contextlib import contextmanager

    keys, clock, disk, sessions, web, sid = seed(tmp_path / "state")
    pair = web.take_session(sid)
    assert pair is not None
    backend = FakeBackend(now_ms=lambda: clock[0])
    attachment, _ = backend.create_attachment(
        "host", "terminal", "shell", "account", "device", takeover=True
    )
    lease = backend.acquire_control(attachment.attachment_id, "account", "device")
    app = create_app(GatewayState(backend, sessions=sessions, now_ms=lambda: clock[0]))
    original = disk.transaction

    @contextmanager
    def interrupted() -> Any:
        with original() as tx:
            yield tx
            if tx.scan(Table.REVOCATIONS, scope=(ORIGIN, "account")).records:
                raise StorageUnavailable("full")

    if failure == "locked":
        keys.locked = True
    else:
        monkeypatch.setattr(disk, "transaction", interrupted)

    async def request() -> None:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url=ORIGIN
        ) as http:
            headers = {"authorization": f"Bearer {pair[0]}"}
            assert (await http.post("/v1/logout", headers=headers)).status_code == 503
            assert not backend.ownership._leases[lease.lease_id].live
            assert sessions.pending_revocation(pair[0]) is not None
            keys.locked = False
            monkeypatch.setattr(disk, "transaction", original)
            assert sessions.lookup(pair[0], clock[0]) is None
            assert sessions.refresh(pair[1], clock[0]) is None
            retry = await http.post("/v1/logout", headers=headers)
            assert retry.status_code == 200 and retry.json() == {"ok": True}
            assert (
                await http.post("/v1/logout", headers={"authorization": "Bearer unknown"})
            ).status_code == 401
            assert sessions.verifier("unknown") not in sessions._denied

    try:
        asyncio.run(request())
    finally:
        keys.locked = False
        monkeypatch.setattr(disk, "transaction", original)
        disk.close()
    disk, sessions, web = stores(tmp_path / "state", keys, clock)
    try:
        assert sessions.lookup(pair[0], clock[0]) is None
        assert web.verify_cookie(web.sign_sid(sid)) is None
    finally:
        disk.close()


def test_refresh_churn_has_no_consumed_pair_tombstones(tmp_path: Path) -> None:
    keys, clock, disk, sessions, web, sid = seed(tmp_path / "state")
    old = web.take_session(sid)
    try:

        async def refresh() -> None:
            for _ in range(1106):
                clock[0] += 1
                assert await web.ensure_access(
                    sid, lambda token: pytest.fail("no HTTP refresh"), force=True
                )

        asyncio.run(refresh())
        with disk.transaction() as tx:
            assert not tx.scan(Table.REVOCATIONS, scope=(ORIGIN, "account")).records
            assert len(tx.scan(Table.GATEWAY_VERIFIERS, scope=(ORIGIN, "account")).records) == 2
        assert sessions.lookup(old[0], clock[0]) is None  # type: ignore[index]
        assert sessions.refresh(old[1], clock[0]) is None  # type: ignore[index]
        current = web.take_session(sid)
        assert current is not None
        clock[0] += 31 * 24 * 60 * 60 * 1000
        disk.sweep(clock[0])
        assert sessions.lookup(current[0], clock[0]) is None
        assert sessions.refresh(current[1], clock[0]) is None
        with disk.transaction() as tx:
            assert not tx.scan(Table.GATEWAY_VERIFIERS, scope=(ORIGIN, "account")).records
    finally:
        disk.close()


def test_flet_cookie_and_identity_reads_leave_event_loop_responsive(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import threading
    from types import SimpleNamespace

    from relaydeck.client import app as client_app
    from relaydeck.client.serve import make_relaydeck_main

    keys, clock, disk, sessions, web, sid = seed(tmp_path / "state")
    started, release = threading.Event(), threading.Event()
    timed_out: list[bool] = []
    threads: list[int] = []
    read = web._read_web

    def blocked(sid: str) -> Any:
        threads.append(threading.get_ident())
        if not started.is_set():
            started.set()
            timed_out.append(not release.wait(0.5))
        return read(sid)

    monkeypatch.setattr(web, "_read_web", blocked)
    state = GatewayState(FakeBackend(), sessions=sessions, now_ms=lambda: clock[0])
    gateway = create_app(state)

    def render(page: Any, **kwargs: Any) -> Any:
        page.add(ft.Text("ready"))
        return SimpleNamespace(close=lambda: None)

    monkeypatch.setattr(client_app, "run_app", render)
    production_main = make_relaydeck_main(
        store=web, base_url=ORIGIN, gateway_app=gateway, gateway_state=state, host_id="host"
    )
    app, _ = build_web_app(
        gateway_state=state,
        gateway_app=gateway,
        flet_main=production_main,
        public_origin=ORIGIN,
        cookie_secret=web.cookie_secret,
        store=web,
    )

    async def request() -> None:
        loop_thread = threading.get_ident()
        async with app.router.lifespan_context(app):
            socket = await CookieSocket(app, f"{COOKIE_NAME}={web.sign_sid(sid)}").open()
            task = asyncio.create_task(socket.register())
            try:
                assert await asyncio.to_thread(started.wait, 2)
                await asyncio.sleep(0)
                release.set()  # The event loop can release the blocked identity read.
                assert isinstance(await asyncio.wait_for(task, 2), str)
                await until(lambda: len(web._closers.get(sid, [])) >= 2)
                assert timed_out == [False]
                assert threads and loop_thread not in threads
            finally:
                release.set()
                await socket.close()

    try:
        asyncio.run(request())
    finally:
        release.set()
        disk.close()


@pytest.mark.parametrize("mode", ["account", "host", "device"])
@pytest.mark.parametrize("failure", ["locked", "commit"])
def test_scope_revocation_failure_severs_writers_before_retry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mode: str, failure: str
) -> None:
    from contextlib import contextmanager

    from starlette.responses import JSONResponse

    from relaydeck.gateway.revocation import revoke_account_and_close, revoke_host_and_close
    from tests.support.gateway.fake_socket import _FakeSocket

    keys, clock, disk, sessions, web, sid = seed(tmp_path / "state")
    pair = web.take_session(sid)
    assert pair is not None
    if mode == "host":
        # A real paired-host grant, including its persisted host restriction.
        pair = sessions.create(
            "account", ["sessions.read"], ["host"], clock[0], "device", host_binding="binding"
        )
    operator = sessions.create("account", ["hosts.enroll"], ["host"], clock[0], "operator")
    foreign = sessions.create("foreign", ["sessions.read"], ["other-host"], clock[0], "other")
    backend = FakeBackend(now_ms=lambda: clock[0])
    attachment, _ = backend.create_attachment(
        "host", "terminal", "agent", "account", "device", takeover=True
    )
    lease = backend.acquire_control(attachment.attachment_id, "account", "device")
    other_attachment, _ = backend.create_attachment(
        "other-host", "other-terminal", "shell", "foreign", "other", takeover=True
    )
    other_lease = backend.acquire_control(other_attachment.attachment_id, "foreign", "other")
    state = GatewayState(backend, sessions=sessions, now_ms=lambda: clock[0])
    state.hosts.enroll("host", "Home", "account")
    viewer, other_viewer, tunnel = _FakeSocket(), _FakeSocket(), _FakeSocket()
    state.sockets.register("account", lease.lease_id, viewer, "device")
    state.sockets.register("foreign", other_lease.lease_id, other_viewer, "other")
    state.sockets.register_host("host", tunnel)
    app = create_app(state)
    original = disk.transaction
    armed = [False]

    @contextmanager
    def interrupted() -> Any:
        if armed[0] and failure == "locked":
            keys.locked = True
        with original() as tx:
            yield tx
            if (
                armed[0]
                and failure == "commit"
                and tx.scan(Table.REVOCATIONS, scope=(ORIGIN, "account")).records
            ):
                raise StorageUnavailable("full")

    monkeypatch.setattr(disk, "transaction", interrupted)
    if mode == "device":
        revoke_device = sessions.revoke_device

        def revoke(account: str, device: str) -> int:
            armed[0] = True  # Fail only after admission authorized the target.
            return revoke_device(account, device)

        monkeypatch.setattr(sessions, "revoke_device", revoke)
    else:
        # Operator helpers have no account-revoke HTTP route. This test-only
        # adapter exercises the production helper and its real 503 handler.
        @app.delete("/test/revoke-scope")
        async def revoke_scope() -> JSONResponse:
            armed[0] = True
            if mode == "account":
                await revoke_account_and_close(state, "account")
            else:
                await revoke_host_and_close(state, "host")
            return JSONResponse({"ok": True})

    async def scenario() -> None:
        _, signal = state.metadata.register("account", "device", "host")
        _, other_signal = state.metadata.register("foreign", "other", "other-host")
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url=ORIGIN
        ) as http:
            path = "/v1/devices/device" if mode == "device" else "/test/revoke-scope"
            headers = {"authorization": f"Bearer {operator[0]}"}
            response = await http.delete(path, headers=headers)
            assert response.status_code == 503
            assert not backend.ownership._leases[lease.lease_id].live
            assert viewer.closes and signal.event.is_set()
            assert backend.ownership._leases[other_lease.lease_id].live
            assert not other_viewer.closes and not other_signal.event.is_set()
            if mode == "host":
                assert tunnel.closes
            armed[0] = False
            keys.locked = False
            monkeypatch.setattr(disk, "transaction", original)
            assert sessions.lookup(pair[0], clock[0]) is None
            assert sessions.refresh(pair[1], clock[0]) is None
            # An authorized operator retries deletion; denied credentials
            # never regain admission merely to retry their revocation.
            retry = await http.delete(path, headers=headers)
            assert retry.status_code == 200 and retry.json() == {"ok": True}
            assert sessions.lookup(pair[0], clock[0]) is None
            assert sessions.refresh(pair[1], clock[0]) is None
            assert sessions.lookup(foreign[0], clock[0]) is not None

    try:
        asyncio.run(scenario())
    finally:
        keys.locked = False
        monkeypatch.setattr(disk, "transaction", original)
        disk.close()


@pytest.mark.parametrize("phase", ["register", "handoff"])
@pytest.mark.parametrize("changed", ["close", "done", "owner", "handoff", "revoke"])
def test_closed_transport_cannot_publish_after_private_read(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, phase: str, changed: str
) -> None:
    import threading
    from types import SimpleNamespace

    import msgpack  # type: ignore[import-untyped]
    from flet.fastapi import app_manager

    from relaydeck.client import serve

    keys, clock, disk, sessions, web, sid = seed(tmp_path / "state")
    handshake = serve._Handshake("192.0.2.10", "synthetic", "")
    entered: list[bool] = []
    published: list[bool] = []
    captured: list[Any] = []
    started, release = threading.Event(), threading.Event()
    original_identity, original_cookie = web.identity, web.verify_cookie
    blocking = [False]
    cookie_reads = [0]

    def identity(owner: str | None) -> Any:
        result = original_identity(owner)
        if blocking[0]:
            started.set()
            assert release.wait(2)
        return result

    def verify_cookie(cookie: str) -> Any:
        result = original_cookie(cookie)
        cookie_reads[0] += 1
        if phase == "handoff" and cookie_reads[0] == 2:
            started.set()
            assert release.wait(2)
        return result

    monkeypatch.setattr(web, "identity", identity)
    monkeypatch.setattr(web, "verify_cookie", verify_cookie)
    if phase == "handoff":
        web.bind_flet(sid, "fid", handshake)

        async def get_session(key: str) -> Any:
            return SimpleNamespace(connection=None)

        monkeypatch.setattr(app_manager, "get_session", get_session)

    async def downstream(scope: Any, receive: Any, send: Any) -> None:
        entered.append(True)
        await receive()
        await receive()
        transport = serve._current_transport.get()
        assert transport is not None
        captured.append(transport)
        web._bind_verified(sid, "fid", handshake, web._sessions[sid].identity)
        blocking[0] = True
        await send(
            {
                "type": "websocket.send",
                "bytes": b"\x00" + msgpack.packb([1, {"session_id": "fid"}]),
            }
        )
        published.append(guard._transports.get("fid") is transport)

    guard = serve.FletGuardMiddleware(downstream, web)

    async def scenario() -> None:
        socket = await CookieSocket(guard, f"{COOKIE_NAME}={web.sign_sid(sid)}").open()
        await socket.packet(
            1,
            {
                "session_id": "fid" if phase == "handoff" else "",
                "page_name": "",
            },
        )
        try:
            assert await asyncio.to_thread(started.wait, 2)
            transport = captured[0] if phase == "register" else guard._handoffs["fid"]
            successor = None
            if changed == "close":
                await transport.close()
            elif changed == "done":
                transport.done.set()
            elif changed == "owner":
                web._owners["fid"] = "replacement-owner"
            elif changed == "handoff":
                successor = serve._FletTransport(lambda: None, asyncio.Event())
                guard._handoffs["fid"] = successor
            else:
                web.begin_revocation(sid)
            release.set()
            assert socket.task is not None
            await asyncio.wait_for(socket.task, 2)
            messages = []
            while not socket.outbound.empty():
                messages.append(socket.outbound.get_nowait())
            assert [item["type"] for item in messages] == ["websocket.close"]
            assert transport.flet_id == "" and transport.done.is_set()
            assert not guard._transports
            assert guard._handoffs == ({"fid": successor} if successor is not None else {})
            if successor is not None:
                assert not successor.closed.is_set() and not successor.done.is_set()
            if changed == "owner":
                assert web.owner_of("fid") == "replacement-owner"
            assert published == ([False] if phase == "register" else [])
            assert entered == ([True] if phase == "register" else [])
        finally:
            release.set()
            await socket.close()

    try:
        asyncio.run(scenario())
    finally:
        release.set()
        disk.close()


def test_revocation_only_lookup_rejects_foreign_origin_scope(tmp_path: Path) -> None:
    keys, clock, disk, sessions, web, sid = seed(tmp_path / "state")
    pair = web.take_session(sid)
    assert pair is not None
    other_origin = "http://foreign"
    other = WebSessionStore(
        other_origin,
        web.cookie_secret,
        now_ms=lambda: clock[0],
        persistence=disk,
        gateway_sessions=sessions,
    )
    state = GatewayState(FakeBackend(), sessions=sessions, now_ms=lambda: clock[0])
    app, _ = build_web_app(
        gateway_state=state,
        gateway_app=create_app(state),
        flet_main=lambda page: None,
        public_origin=other_origin,
        cookie_secret=other.cookie_secret,
        store=other,
    )

    async def request() -> None:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url=other_origin
        ) as http:
            result = await http.post(
                "/v1/auth/logout",
                headers={"origin": other_origin, "cookie": f"{COOKIE_NAME}={web.sign_sid(sid)}"},
            )
            assert (
                result.status_code == 204
            )  # Durable read proves absence in this scope; no foreign mutation.
        assert web.verify_cookie(web.sign_sid(sid)) == sid
        assert sessions.lookup(pair[0], clock[0]) is not None
        assert not sessions._denied
        with disk.transaction() as tx:
            assert not tx.scan(Table.REVOCATIONS, scope=(ORIGIN, "account")).records

    try:
        asyncio.run(request())
    finally:
        disk.close()


def test_late_private_read_cannot_rebind_replacement_flet_owner(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import threading
    from types import SimpleNamespace

    from relaydeck.client import serve

    keys, clock, disk, sessions, web, sid = seed(tmp_path / "state")
    pair = sessions.create("replacement", ["sessions.read"], ["host"], clock[0], "new-device")
    other_sid = web.create_session("replacement", "new-device", *pair, clock[0] + 900_000)
    old_handshake = serve._Handshake("192.0.2.1", "old", "")
    new_handshake = serve._Handshake("192.0.2.2", "new", "")
    web.bind_flet(sid, "fid", old_handshake)
    started, release = threading.Event(), threading.Event()
    original = web.identity

    def blocked(owner: str | None) -> Any:
        if owner == sid:
            started.set()
            assert release.wait(2)
        return original(owner)

    monkeypatch.setattr(web, "identity", blocked)

    async def request() -> None:
        closed = asyncio.Event()
        transport = serve._FletTransport(lambda: None, closed, flet_id="fid")
        handshake_token = serve._current_handshake.set((sid, old_handshake))
        transport_token = serve._current_transport.set(transport)
        try:
            task = asyncio.create_task(
                serve.rebind_on_connect(web, SimpleNamespace(session=SimpleNamespace(id="fid")))
            )
        finally:
            serve._current_handshake.reset(handshake_token)
            serve._current_transport.reset(transport_token)
        assert await asyncio.to_thread(started.wait, 2)
        closed.set()
        # A newer, verified handoff has already installed its ephemeral owner.
        web._owners["fid"] = other_sid
        web._handshakes["fid"] = new_handshake
        release.set()
        await task
        assert web.owner_of("fid") == other_sid
        assert web._handshakes["fid"] == new_handshake
        assert "fid" not in web._draft_tabs

    try:
        asyncio.run(request())
    finally:
        release.set()
        disk.close()
