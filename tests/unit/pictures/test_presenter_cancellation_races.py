"""Presenter proof and cancellation/retention races (synthetic local data)."""

from __future__ import annotations

import asyncio
import threading
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import httpx
import pytest

from relaydeck.companion.pictures import DAY_MS, PictureStore
from relaydeck.contracts.pairing import DEVICE_CAPABILITIES
from relaydeck.gateway.app import create_app
from tests.security.helpers import auth, make_state
from tests.support.pictures.uploads import picture


def test_copied_grant_requires_exact_presenter_session(tmp_path: Path) -> None:
    async def scenario() -> None:
        state, _ = make_state()
        store = state.pictures = PictureStore(tmp_path)
        owner, _ = state.sessions.create(
            "a", DEVICE_CAPABILITIES, {"home-mac"}, state.now_ms(), device_id="one"
        )
        device, _ = state.sessions.create(
            "a", DEVICE_CAPABILITIES, {"home-mac"}, state.now_ms(), device_id="two"
        )
        account, _ = state.sessions.create("foreign", (), (), state.now_ms(), device_id="three")
        same_device, _ = state.sessions.create(
            "a", DEVICE_CAPABILITIES, {"home-mac"}, state.now_ms(), device_id="one"
        )
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=create_app(state)), base_url="https://test"
        ) as client:
            grant = (
                await client.post("/v1/pictures", headers=auth(owner), json={"host_id": "home-mac"})
            ).json()
            for presenter in (None, device, account, same_device):
                response = await client.put(
                    grant["upload_url"],
                    content=picture(),
                    headers=auth(presenter) if presenter else {},
                )
                assert response.status_code == 401
                assert response.headers["cache-control"] == "private, no-store"
                assert store.uploads[grant["picture_id"]].status == "pending"
                assert not list(tmp_path.iterdir())
            assert (
                await client.put(grant["upload_url"], headers=auth(owner), content=picture())
            ).status_code == 204
            assert (
                await client.put(grant["upload_url"], headers=auth(owner), content=picture())
            ).status_code == 400

    asyncio.run(scenario())


def test_web_cookie_adapter_authenticates_actual_presenter(tmp_path: Path) -> None:
    from relaydeck.client.serve import WebSessionStore, build_web_app

    async def scenario() -> None:
        state, _ = make_state()
        state.pictures = PictureStore(tmp_path)
        origin = "https://mac.test"
        web = WebSessionStore(origin, b"synthetic-cookie-key" * 2, now_ms=state.now_ms)
        identities = [
            ("a", "one", DEVICE_CAPABILITIES),
            ("a", "two", DEVICE_CAPABILITIES),
            ("b", "three", ()),
            ("a", "one", DEVICE_CAPABILITIES),
        ]
        tokens, cookies = [], []
        for account, device, caps in identities:
            access, refresh = state.sessions.create(
                account, caps, {"home-mac"}, state.now_ms(), device_id=device
            )
            sid = web.create_session(account, device, access, refresh, state.now_ms() + 900_000)
            tokens.append(access)
            cookies.append("rd_session=" + web.sign_sid(sid))
        app, _ = build_web_app(
            gateway_state=state,
            gateway_app=create_app(state),
            flet_main=lambda page: None,
            public_origin=origin,
            cookie_secret=web.cookie_secret,
            store=web,
        )
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url=origin
        ) as client:
            grant = (
                await client.post(
                    "/v1/pictures", headers=auth(tokens[0]), json={"host_id": "home-mac"}
                )
            ).json()
            invalid = [
                {},
                {"origin": origin},
                {"origin": origin, "cookie": "rd_session=invalid"},
                {"origin": "https://foreign.test", "cookie": cookies[0]},
                {"cookie": cookies[0]},
                *({"origin": origin, "cookie": cookie} for cookie in cookies[1:]),
                {"origin": origin, "cookie": cookies[0], **auth(tokens[1])},
            ]
            for headers in invalid:
                denied = await client.put(grant["upload_url"], content=picture(), headers=headers)
                assert denied.status_code == 401
                assert denied.headers["cache-control"] == "private, no-store"
                assert state.pictures.get(grant["picture_id"]).status == "pending"
            accepted = await client.put(
                grant["upload_url"],
                content=picture(),
                headers={"origin": origin, "cookie": cookies[0]},
            )
            assert accepted.status_code == 204
            assert (
                await client.get(f"/v1/pictures/{grant['picture_id']}", headers=auth(tokens[0]))
            ).json()["status"] == "ready"
            again = (
                await client.post(
                    "/v1/pictures", headers=auth(tokens[0]), json={"host_id": "home-mac"}
                )
            ).json()
            state.sessions.revoke_session(tokens[0])
            assert (
                await client.put(
                    again["upload_url"],
                    content=picture(),
                    headers={"origin": origin, "cookie": cookies[0]},
                )
            ).status_code == 401

    asyncio.run(scenario())


def test_native_adapter_sends_proof_and_never_reads_files_in_web_runtime(tmp_path: Path) -> None:
    import flet as ft

    from relaydeck.client.features.shell.ops import HttpxShellGatewayOps
    from relaydeck.client.features.terminal.pictures import PictureClientError

    async def scenario() -> None:
        state, _ = make_state()
        state.pictures = PictureStore(tmp_path / "private")
        token, _ = state.sessions.create(
            "a", DEVICE_CAPABILITIES, {"home-mac"}, state.now_ms(), device_id="one"
        )

        async def get_token() -> str:
            return token

        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=create_app(state)), base_url="https://mac.test"
        ) as client:
            native = HttpxShellGatewayOps(
                "https://mac.test", get_token, client, native_picture_upload=True
            ).picture_api
            web = HttpxShellGatewayOps("https://mac.test", get_token, client).picture_api
            selected = tmp_path / "selected.png"
            selected.write_bytes(picture())
            file = ft.FilePickerFile(7, "spoof.exe", selected.stat().st_size, str(selected))
            progress: list[float] = []
            grant = await native.begin("home-mac")
            await native.upload_native(grant, file, lambda: True, progress.append)
            assert (
                progress[-1] == 1
                and (await native.result(grant["picture_id"]))["status"] == "ready"
            )
            # Neither a forged native Page nor a picked path authorizes server-local reads.
            with pytest.raises(PictureClientError):
                await web.upload_native(
                    grant, SimpleNamespace(path=str(selected)), lambda: True, progress.append
                )
            stale = await native.begin("home-mac")
            with pytest.raises(PictureClientError):
                await native.upload_native(stale, file, lambda: False, progress.append)
            assert (await native.result(stale["picture_id"]))["status"] == "pending"

    asyncio.run(scenario())


def test_concurrent_redemption_claims_grant_once(tmp_path: Path) -> None:
    async def scenario() -> None:
        state, _ = make_state()
        state.pictures = PictureStore(tmp_path)
        token, _ = state.sessions.create(
            "a", DEVICE_CAPABILITIES, {"home-mac"}, state.now_ms(), device_id="one"
        )
        entered, proceed = asyncio.Event(), asyncio.Event()
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=create_app(state)), base_url="https://test"
        ) as client:
            grant = (
                await client.post("/v1/pictures", headers=auth(token), json={"host_id": "home-mac"})
            ).json()

            async def stream():
                yield picture()[:16]
                entered.set()
                await proceed.wait()
                yield picture()[16:]

            first = asyncio.create_task(
                client.put(grant["upload_url"], headers=auth(token), content=stream())
            )
            await asyncio.wait_for(entered.wait(), 3)
            try:
                replay = await client.put(
                    grant["upload_url"], headers=auth(token), content=picture()
                )
                assert replay.status_code == 400
            finally:
                proceed.set()
            assert (await first).status_code == 204
            assert len(list(tmp_path.iterdir())) == 1

    asyncio.run(scenario())


@pytest.mark.parametrize("boundary", ["cancel", "expiry"])
def test_directory_scan_tolerates_concurrent_removal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, boundary: str
) -> None:
    store = PictureStore(tmp_path)
    identifier, record = store.begin("a", "one", "mac", "synthetic", 1000)
    record.path, record.preview = store.save_with_preview(picture(), 1000)
    record.status, record.expires_ms = "ready", DAY_MS + 1000
    doomed = Path(record.path)
    reached, proceed = threading.Event(), threading.Event()
    lstat = Path.lstat

    def delayed_lstat(path: Path):
        if path == doomed and not reached.is_set():
            reached.set()
            assert proceed.wait(3), "cleanup scan did not resume"
        return lstat(path)

    monkeypatch.setattr(Path, "lstat", delayed_lstat)

    async def scenario() -> None:
        cleanup = asyncio.create_task(asyncio.to_thread(store.cleanup, 1001))
        try:
            assert await asyncio.to_thread(reached.wait, 3)
            if boundary == "cancel":
                store.cancel(identifier)
            else:
                store._expire(DAY_MS + 1000)
        finally:
            proceed.set()
        await cleanup
        assert record.preview == record.path == ""
        assert identifier not in store.uploads
        # A subsequent pass must still remove both registered and old-process files.
        other, ready = store.begin("a", "one", "mac", "synthetic", DAY_MS + 1001)
        ready.path, ready.preview = store.save_with_preview(picture(), DAY_MS + 1001)
        ready.status, ready.expires_ms = "ready", 2 * DAY_MS + 1001
        survivor = Path(ready.path)
        orphan = Path(store.save(picture(), 1000))
        store.cleanup(2 * DAY_MS + 1001)
        assert not survivor.exists() and not orphan.exists()
        assert other not in store.uploads and ready.preview == ready.path == ""

    asyncio.run(scenario())


@pytest.mark.parametrize("failure", ["recoverable", "unsafe_root"])
def test_lifespan_keeps_scheduled_cleanup_alive_after_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failure: str
) -> None:
    from relaydeck.companion import pictures

    async def scenario() -> None:
        state, _ = make_state(now=1000)
        clock = [1000]
        state.now_ms = lambda: clock[0]
        store = state.pictures = PictureStore(tmp_path)
        identifier, record = store.begin("a", "one", "mac", "synthetic", 1000)
        record.path, record.preview = store.save_with_preview(picture(), 1000)
        record.status, record.expires_ms = "ready", DAY_MS + 1000
        original = Path(record.path)
        orphan = Path(store.save(picture(), 1000))
        waiting: asyncio.Queue[Any] = asyncio.Queue()
        cleanup = store.cleanup
        passes = 0

        def interrupted(now: int) -> None:
            nonlocal passes
            passes += 1
            if passes == 2 and failure == "recoverable":
                raise PermissionError("private-image-path-and-metadata-must-not-be-logged")
            cleanup(now)

        async def controlled_sleep(seconds: float) -> None:
            assert seconds == 3600
            tick = asyncio.Event()
            waiting.put_nowait(tick)
            await tick.wait()

        runner = pictures.run_picture_cleanup

        async def scheduled(store: PictureStore, now: Any) -> None:
            await runner(store, now, sleep=controlled_sleep)

        monkeypatch.setattr(store, "cleanup", interrupted)
        monkeypatch.setattr(pictures, "run_picture_cleanup", scheduled)
        app = create_app(state)
        async with app.router.lifespan_context(app):
            tick = await asyncio.wait_for(waiting.get(), 3)
            clock[0] = DAY_MS + 1000
            if failure == "unsafe_root":
                tmp_path.chmod(0o755)
            tick.set()
            next_tick = await asyncio.wait_for(waiting.get(), 3)
            assert store.cleanup_error == (
                "Picture cleanup deferred; retry scheduled."
                if failure == "recoverable"
                else "Picture storage is unsafe; cleanup refused."
            )
            assert original.exists() and orphan.exists() and record.preview
            tmp_path.chmod(0o700)
            next_tick.set()
            await asyncio.wait_for(waiting.get(), 3)
            assert passes == 3 and store.cleanup_error == ""
            assert not original.exists() and not orphan.exists()
            assert record.path == record.preview == "" and identifier not in store.uploads

    asyncio.run(scenario())
