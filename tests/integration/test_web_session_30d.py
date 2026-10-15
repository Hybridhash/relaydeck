"""The web/PWA sign-in is sliding (30 days without use), capped at 90 days from
pairing, extended only by a verified live use (the token rotation, in the same transaction),
never by a restore, and the cookie Max-Age follows. Synthetic private storage and clock."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest

from relaydeck.client.serve import (
    COOKIE_NAME,
    SESSION_MAX_S,
    SESSION_TTL_S,
    SessionCookieRenewalMiddleware,
    WebSessionStore,
)
from relaydeck.companion.persistence import open_store
from relaydeck.gateway.identity import SessionStore
from tests.support.persistence.persistence import Config, MemoryKeys

ORIGIN = "http://test"
DAY = 86_400_000
HOUR = 3_600_000
MIN = 60_000


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


async def _never(_refresh: str) -> Any:  # the durable path rotates through the authority
    raise AssertionError("durable rotation does not call the refresher")


def _use(web: WebSessionStore, sid: str) -> str | None:
    """One verified live use: the access token is stale, so it rotates (and may extend)."""
    return asyncio.run(web.ensure_access(sid, _never, force=True))


def _max_age(headers: dict[str, str]) -> int:
    cookie = headers["Set-Cookie"]
    return int(cookie.split("Max-Age=")[1].split(";")[0])


def test_the_cookie_at_pairing_lasts_30_days(tmp_path: Path) -> None:
    _keys, clock, disk, _sessions, web, sid = seed(tmp_path / "state")
    try:
        assert SESSION_TTL_S == 30 * 24 * 3600 and SESSION_MAX_S == 90 * 24 * 3600
        assert web._sessions[sid].expires_ms == clock[0] + 30 * DAY
        assert _max_age(web.cookie_headers(sid)) == SESSION_TTL_S
    finally:
        disk.close()


def test_use_at_day_29_extends_so_it_is_still_valid_at_day_45(tmp_path: Path) -> None:
    _keys, clock, disk, _sessions, web, sid = seed(tmp_path / "state")
    try:
        clock[0] += 29 * DAY
        assert web.identity(sid) is not None
        assert _use(web, sid) is not None
        assert web._sessions[sid].expires_ms == clock[0] + 30 * DAY
        clock[0] += 16 * DAY  # day 45: past the original 30 days
        assert web.identity(sid) is not None
        assert web.verify_cookie(web.sign_sid(sid)) == sid
    finally:
        disk.close()


def test_thirty_days_without_use_is_expired(tmp_path: Path) -> None:
    _keys, clock, disk, _sessions, web, sid = seed(tmp_path / "state")
    try:
        clock[0] += 30 * DAY + 1
        assert web.identity(sid) is None
        assert web.verify_cookie(web.sign_sid(sid)) is None
    finally:
        disk.close()


def test_the_90_day_cap_holds_even_with_daily_use(tmp_path: Path) -> None:
    _keys, clock, disk, _sessions, web, sid = seed(tmp_path / "state")
    paired = clock[0]
    try:
        for _day in range(1, 90):
            clock[0] += DAY
            assert _use(web, sid) is not None, f"day {_day}"
            assert web._sessions[sid].expires_ms <= paired + 90 * DAY
        # Day 60 on, the expiry sits at the cap, not at now + 30 days.
        assert web._sessions[sid].expires_ms == paired + 90 * DAY
        clock[0] = paired + 90 * DAY + 1
        assert web.identity(sid) is None
        assert _use(web, sid) is None  # a re-pair is required
    finally:
        disk.close()


def test_extension_is_throttled_to_once_an_hour(tmp_path: Path) -> None:
    _keys, clock, disk, _sessions, web, sid = seed(tmp_path / "state")
    try:
        first = web._sessions[sid].expires_ms
        for _ in range(3):  # three rotations inside the first hour
            clock[0] += 16 * MIN
            assert _use(web, sid) is not None
        assert web._sessions[sid].expires_ms == first  # no extension write yet
        clock[0] += 20 * MIN  # now over an hour since pairing
        assert _use(web, sid) is not None
        moved = web._sessions[sid].expires_ms
        assert moved == clock[0] + 30 * DAY > first
        clock[0] += 16 * MIN
        assert _use(web, sid) is not None
        assert web._sessions[sid].expires_ms == moved  # throttled again
    finally:
        disk.close()


def test_restore_never_extends_but_a_verified_use_after_restore_does(tmp_path: Path) -> None:
    keys, clock, disk, sessions, web, sid = seed(tmp_path / "state")
    cookie = web.sign_sid(sid)
    saved = web._sessions[sid].expires_ms
    disk.close()
    clock[0] += 10 * DAY
    disk, sessions, restored = stores(tmp_path / "state", keys, clock)
    try:
        assert not restored._sessions
        assert restored.verify_cookie(cookie) == sid  # restore
        assert restored._sessions[sid].expires_ms == saved  # not extended by being restored
        assert restored.identity(sid) is not None
        assert _use(restored, sid) is not None  # a verified live use
        assert restored._sessions[sid].expires_ms == clock[0] + 30 * DAY
        # The extension was persisted with the rotation: a second restart sees it.
        disk.close()
        clock[0] += 1000
        disk, sessions, again = stores(tmp_path / "state", keys, clock)
        assert again.verify_cookie(cookie) == sid
        assert again._sessions[sid].expires_ms == restored._sessions[sid].expires_ms
    finally:
        disk.close()


def test_logout_revokes_immediately_and_stays_revoked(tmp_path: Path) -> None:
    keys, clock, disk, _sessions, web, sid = seed(tmp_path / "state")
    cookie = web.sign_sid(sid)
    try:
        clock[0] += 29 * DAY
        assert _use(web, sid) is not None  # extended, long-lived
        web.begin_revocation(sid)
        assert web.revoke_sid(sid) is not None
        assert web.identity(sid) is None and web.verify_cookie(cookie) is None
        assert web.cookie_renewal(cookie) is None  # nothing re-issued for a revoked session
        disk.close()
        disk, _sessions, restarted = stores(tmp_path / "state", keys, clock)
        assert restarted.verify_cookie(cookie) is None
    finally:
        disk.close()


def test_the_cookie_max_age_follows_the_extension_and_is_reissued_once(tmp_path: Path) -> None:
    _keys, clock, disk, _sessions, web, sid = seed(tmp_path / "state")
    cookie = web.sign_sid(sid)
    try:
        web.cookie_headers(sid)  # issued at pairing
        assert web.cookie_renewal(cookie) is None  # nothing changed
        clock[0] += 5 * DAY
        assert _use(web, sid) is not None
        renewal = web.cookie_renewal(cookie)
        assert renewal is not None and _max_age(renewal) == SESSION_TTL_S
        assert web.cookie_renewal(cookie) is None  # reissued once, not on every request
        # An unsigned or foreign cookie gets nothing.
        assert web.cookie_renewal("nope") is None
        assert web.cookie_renewal(cookie[:-1] + ("0" if cookie[-1] != "0" else "1")) is None
    finally:
        disk.close()


def test_the_middleware_adds_the_renewed_cookie_to_a_get_response(tmp_path: Path) -> None:
    _keys, clock, disk, _sessions, web, sid = seed(tmp_path / "state")
    cookie = web.sign_sid(sid)
    try:
        web.cookie_headers(sid)
        clock[0] += 5 * DAY
        assert _use(web, sid) is not None
        sent: list[dict[str, Any]] = []

        async def downstream(scope: Any, receive: Any, send: Any) -> None:
            await send({"type": "http.response.start", "status": 200, "headers": []})
            await send({"type": "http.response.body", "body": b""})

        async def send(message: dict[str, Any]) -> None:
            sent.append(message)

        async def receive() -> dict[str, Any]:
            return {"type": "http.request"}

        middleware = SessionCookieRenewalMiddleware(downstream, web)
        scope = {
            "type": "http",
            "method": "GET",
            "path": "/",
            "headers": [(b"cookie", f"{COOKIE_NAME}={cookie}".encode())],
        }
        asyncio.run(middleware(scope, receive, send))
        headers = dict(sent[0]["headers"])
        assert b"Max-Age=" + str(SESSION_TTL_S).encode() in headers[b"set-cookie"]
        sent.clear()
        asyncio.run(middleware(scope, receive, send))  # already reissued
        assert sent[0]["headers"] == []
    finally:
        disk.close()


def test_an_extension_never_outlives_the_refresh_grant_or_the_cap(tmp_path: Path) -> None:
    _keys, clock, disk, _sessions, web, sid = seed(tmp_path / "state")
    try:
        session = web._sessions[sid]
        session.expires_ms = clock[0] + 1 * DAY  # near its end, so an extension would move it
        later = clock[0] + 2 * HOUR
        short = clock[0] + 5 * DAY
        assert web._extension(session, later, short) == (short, later)  # capped by the grant
        far = clock[0] + 400 * DAY
        session.paired_ms = clock[0] - 80 * DAY
        moved, _ = web._extension(session, later, far)
        assert moved == session.paired_ms + 90 * DAY  # capped by the absolute limit
    finally:
        disk.close()


def test_legacy_rows_without_pairing_time_are_not_extended_by_restore() -> None:
    expires = 1_900_000_000_000
    paired, extended = WebSessionStore._lifetime({"expires_ms": expires})
    assert paired == expires - 12 * 3600 * 1000 and extended == paired


@pytest.mark.parametrize("persistent", [False])
def test_without_durable_storage_the_in_memory_session_slides_too(persistent: bool) -> None:
    clock = [1_900_000_000_000]
    web = WebSessionStore("http://test", b"k" * 32, now_ms=lambda: clock[0])
    sid = web.create_session("acct", "dev", "access", "refresh", clock[0] + 15 * MIN)
    paired = clock[0]

    async def refresher(refresh: str) -> Any:
        return "access2", "refresh2", 900

    clock[0] += 3 * HOUR
    assert asyncio.run(web.ensure_access(sid, refresher, force=True)) == "access2"
    assert web._sessions[sid].expires_ms == clock[0] + 30 * DAY
    clock[0] = paired + 30 * DAY + HOUR  # past the original 30 days, inside the slide
    assert web.identity(sid) is not None
    clock[0] = paired + 3 * HOUR + 30 * DAY + 1
    assert web.identity(sid) is None
