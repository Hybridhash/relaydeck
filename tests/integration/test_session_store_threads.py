"""Durable sign-in reads run on worker threads, so the session store locks its shared maps."""

from __future__ import annotations

import ast
import asyncio
import inspect
import textwrap
import threading
from pathlib import Path
from typing import Any

import httpx
import pytest

from relaydeck.companion.persistence import open_store
from relaydeck.contracts import paths
from relaydeck.gateway import identity
from relaydeck.gateway.app import GatewayState, create_app
from relaydeck.gateway.backend import FakeBackend
from relaydeck.gateway.identity import SessionStore
from tests.support.persistence.persistence import Config, MemoryKeys

NOW = 1_900_000_000_000


def durable(path: Path) -> tuple[Any, SessionStore]:
    keys = MemoryKeys()
    keys.restore_generation("authentication", create=True)
    disk = open_store(Config(path), keys)
    sessions = SessionStore(
        persistence=disk,
        origin="http://test",
        generation_reader=lambda: keys.restore_generation("authentication").value,
    )
    return disk, sessions


SHARED = {
    "_by_access",
    "_refresh_to_access",
    "_denied",
    "_denied_scopes",
    "_known_pairs",
    "_pending_revocations",
}


def test_every_method_touching_shared_maps_holds_the_lock() -> None:
    tree = ast.parse(textwrap.dedent(inspect.getsource(identity.SessionStore)))
    unlocked = []
    for node in tree.body[0].body:  # type: ignore[attr-defined]
        if not isinstance(node, ast.FunctionDef) or node.name == "__post_init__":
            continue
        touches = any(
            isinstance(sub, ast.Attribute)
            and sub.attr in SHARED
            and isinstance(sub.value, ast.Name)
            and sub.value.id == "self"
            for sub in ast.walk(node)
        )
        locked = any(isinstance(d, ast.Name) and d.id == "_locked" for d in node.decorator_list)
        if touches and not locked:
            unlocked.append(node.name)
    assert unlocked == []


def test_a_held_lock_makes_another_thread_wait(tmp_path: Path) -> None:
    disk, sessions = durable(tmp_path / "state")
    access, _refresh = sessions.create("account", ["sessions.read"], ["host"], NOW, "device")
    done = threading.Event()
    try:
        with sessions._lock:
            worker = threading.Thread(target=lambda: (sessions.lookup(access, NOW), done.set()))
            worker.start()
            assert not done.wait(0.2)
        assert done.wait(2)
        worker.join(2)
    finally:
        disk.close()


def _blocking(
    sessions: SessionStore, name: str, monkeypatch: pytest.MonkeyPatch
) -> tuple[threading.Event, threading.Event, list[int]]:
    started, release, threads = threading.Event(), threading.Event(), []
    original = getattr(sessions, name)

    def blocked(*args: Any, **kwargs: Any) -> Any:
        threads.append(threading.get_ident())
        started.set()
        assert release.wait(2)
        return original(*args, **kwargs)

    monkeypatch.setattr(sessions, name, blocked)
    return started, release, threads


async def _call_while_blocked(
    app: Any,
    started: threading.Event,
    release: threading.Event,
    method: str,
    url: str,
    **kwargs: Any,
) -> tuple[httpx.Response, int]:
    loop_thread = threading.get_ident()
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        task = asyncio.create_task(client.request(method, url, **kwargs))
        assert await asyncio.to_thread(started.wait, 2)
        await asyncio.sleep(0)  # the loop is still free while the read is blocked
        release.set()
        return await asyncio.wait_for(task, 2), loop_thread


def test_durable_lookup_runs_off_the_event_loop(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    disk, sessions = durable(tmp_path / "state")
    access, _refresh = sessions.create("account", ["sessions.read"], ["host"], NOW, "device")
    started, release, threads = _blocking(sessions, "lookup", monkeypatch)
    app = create_app(GatewayState(FakeBackend(), sessions=sessions, now_ms=lambda: NOW))
    try:
        response, loop_thread = asyncio.run(
            _call_while_blocked(
                app,
                started,
                release,
                "GET",
                paths.ME,
                headers={"authorization": f"Bearer {access}"},
            )
        )
        assert response.status_code == 200
        assert threads and loop_thread not in threads
    finally:
        release.set()
        disk.close()


def test_durable_refresh_runs_off_the_event_loop(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    disk, sessions = durable(tmp_path / "state")
    _access, refresh = sessions.create("account", ["sessions.read"], ["host"], NOW, "device")
    started, release, threads = _blocking(sessions, "refresh", monkeypatch)
    app = create_app(GatewayState(FakeBackend(), sessions=sessions, now_ms=lambda: NOW))
    try:
        response, loop_thread = asyncio.run(
            _call_while_blocked(
                app,
                started,
                release,
                "POST",
                paths.TOKEN,
                json={"grant": "refresh_token", "refresh_token": refresh},
            )
        )
        assert response.status_code == 200
        assert set(response.json()) == {"access_token", "refresh_token", "expires_in"}
        assert threads and loop_thread not in threads
    finally:
        release.set()
        disk.close()


def test_durable_device_revoke_runs_off_the_event_loop(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    disk, sessions = durable(tmp_path / "state")
    access, _refresh = sessions.create("account", ["sessions.read"], [], NOW, "device")
    started, release, threads = _blocking(sessions, "revoke_device", monkeypatch)
    app = create_app(GatewayState(FakeBackend(), sessions=sessions, now_ms=lambda: NOW))
    try:
        response, loop_thread = asyncio.run(
            _call_while_blocked(
                app,
                started,
                release,
                "DELETE",
                paths.device("device"),
                headers={"authorization": f"Bearer {access}"},
            )
        )
        assert response.status_code == 200
        assert threads and loop_thread not in threads
    finally:
        release.set()
        disk.close()
