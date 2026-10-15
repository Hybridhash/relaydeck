"""device-local `haptics` pref and the Send/Copy tap wrappers.

Flet-free: the service is a recording double. A raising or missing service never breaks Send or
Copy, and a tap is fired only when the action actually happened.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from relaydeck.client.features.composer.domain import (
    SEND_DELIVERED_VIA_PROMPT,
    SEND_IGNORED,
)
from relaydeck.client.features.settings import prefs as prefs_store
from relaydeck.client.features.settings.prefs import PREFS_KEY, TerminalPrefs
from relaydeck.client.features.terminal import haptics


class Recorder:
    def __init__(self, fail: bool = False) -> None:
        self.calls = 0
        self.fail = fail

    async def light_impact(self) -> None:
        self.calls += 1
        if self.fail:
            raise RuntimeError("unsupported platform")


async def _settle() -> None:
    for _ in range(3):
        await asyncio.sleep(0)


def test_haptics_pref_defaults_on_and_round_trips() -> None:
    assert TerminalPrefs().haptics is True
    assert prefs_store.clean({"haptics": False}) == TerminalPrefs(haptics=False)
    assert prefs_store.clean({"haptics": "no"}) == TerminalPrefs()  # non-bool: default
    store: dict[str, str] = {}

    async def scenario() -> None:
        async def put(key: str, value: str) -> bool:
            store[key] = value
            return True

        async def get(key: str) -> Any:
            return store.get(key)

        assert await prefs_store.save(put, TerminalPrefs(haptics=False)) is True
        assert '"haptics": false' in store[PREFS_KEY]
        assert await prefs_store.load(get) == TerminalPrefs(haptics=False)

    asyncio.run(scenario())


def test_tap_fires_one_light_impact_only_when_enabled_and_available() -> None:
    async def scenario() -> None:
        on, off = Recorder(), Recorder()
        haptics.tap(on, True)
        haptics.tap(off, False)
        haptics.tap(None, True)
        await _settle()
        assert on.calls == 1
        assert off.calls == 0

    asyncio.run(scenario())


def test_raising_service_is_swallowed() -> None:
    async def scenario() -> None:
        broken = Recorder(fail=True)
        haptics.tap(broken, True)
        await _settle()
        assert broken.calls == 1

    asyncio.run(scenario())


def test_send_taps_at_the_tap_before_the_send_starts_only_when_ready(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = Recorder()
    monkeypatch.setattr(haptics, "page_haptic_service", lambda: service)

    async def scenario() -> None:
        gate = asyncio.Event()
        seen_inside_send: list[int] = []
        ready = {"value": True}

        async def slow_delivery() -> str:
            await asyncio.sleep(0)  # the first suspension lets the tap task run
            seen_inside_send.append(service.calls)
            await gate.wait()  # delivery over the relay can take tens of seconds
            return SEND_DELIVERED_VIA_PROMPT

        task = asyncio.ensure_future(haptics.on_send(slow_delivery, True, lambda: ready["value"])())
        await _settle()
        assert seen_inside_send == [1]  # tapped before the send completed
        assert service.calls == 1
        assert not task.done()
        gate.set()
        assert await task == SEND_DELIVERED_VIA_PROMPT
        assert service.calls == 1

        async def ignored() -> str:
            return SEND_IGNORED

        ready["value"] = False  # e.g. empty draft, sending, or a pending review
        assert await haptics.on_send(ignored, True, lambda: ready["value"])() == SEND_IGNORED
        await _settle()
        assert service.calls == 1

        off = Recorder()
        monkeypatch.setattr(haptics, "page_haptic_service", lambda: off)
        assert await haptics.on_send(ignored, False, lambda: True)() == SEND_IGNORED
        await _settle()
        assert off.calls == 0

    asyncio.run(scenario())


def test_copy_tap_after_success_only_and_failure_still_propagates(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = Recorder()
    monkeypatch.setattr(haptics, "page_haptic_service", lambda: service)

    async def scenario() -> None:
        async def copy(anchor: int, focus: int) -> str:
            return "copied"

        async def failing(anchor: int, focus: int) -> str:
            raise RuntimeError("clipboard refused")

        assert await haptics.on_copy(copy, True)(0, 1) == "copied"
        await _settle()
        assert service.calls == 1
        try:
            await haptics.on_copy(failing, True)(0, 1)
        except RuntimeError:
            pass
        else:
            raise AssertionError("copy failure must still propagate")
        await _settle()
        assert service.calls == 1

    asyncio.run(scenario())


def test_phone_copy_taps_only_when_something_was_copied(monkeypatch: pytest.MonkeyPatch) -> None:
    service = Recorder()
    monkeypatch.setattr(haptics, "page_haptic_service", lambda: service)

    async def scenario() -> None:
        async def copied(mode: str) -> str | None:
            return "text"

        async def not_live(mode: str) -> str | None:
            return None

        assert await haptics.on_phone_copy(copied, True)("all") == "text"
        assert await haptics.on_phone_copy(not_live, True)("all") is None
        await _settle()
        assert service.calls == 1

    asyncio.run(scenario())


def test_a_raising_service_never_breaks_send_or_copy(monkeypatch: pytest.MonkeyPatch) -> None:
    broken = Recorder(fail=True)
    monkeypatch.setattr(haptics, "page_haptic_service", lambda: broken)

    async def scenario() -> None:
        async def delivered() -> str:
            return SEND_DELIVERED_VIA_PROMPT

        async def copied(anchor: int, focus: int) -> str:
            return "text"

        assert await haptics.on_send(delivered, True, lambda: True)() == SEND_DELIVERED_VIA_PROMPT
        assert await haptics.on_copy(copied, True)(0, 1) == "text"
        await _settle()
        assert broken.calls == 2

    asyncio.run(scenario())
