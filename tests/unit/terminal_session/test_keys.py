"""Q5e: KeyInput alone, over the real generation-bound KeyInputSession."""

from __future__ import annotations

import asyncio
from typing import Any

import pytest
from relaydeck_terminal.protocol import ORIGIN_KEY, ORIGIN_MOUSE, ORIGIN_TEXT

from relaydeck.client.features.composer.domain import ComposerStore
from relaydeck.client.features.terminal.input_modes import (
    ROUTE_DRAFT,
    ROUTE_DROPPED,
    ROUTE_PTY,
    WORKFLOW_AGENT_COMPOSER,
    WORKFLOW_AGENT_DIRECT,
    WORKFLOW_SHELL,
    InputModeState,
)
from relaydeck.client.features.terminal.keys import KeyInputSession
from relaydeck.client.features.terminal.session.keys import KeyHooks, KeyInput


async def _submit(_command: dict[str, Any]) -> dict[str, Any]:
    return {"ok": True}


async def _prompt(_agent: str, _text: str) -> dict[str, Any]:
    return {"ok": True}


def make_store() -> ComposerStore:
    return ComposerStore(
        target="sess:1",
        submit_command=_submit,
        prompt_text=_prompt,
        host_id="host-1",
        host_epoch="epoch-1",
        agent_id="agent-1",
    )


class Rig:
    def __init__(self, workflow: str = WORKFLOW_SHELL, *, agent: bool = False) -> None:
        self.generation: int | None = 1
        self.sent: list[bytes] = []
        self.notified = 0
        self.bound = True
        self.paused = False
        self.foreground = True
        self.epoch = 1
        self.store = make_store() if agent else None
        self.modes = InputModeState(workflow=workflow, store=self.store)
        self.session = KeyInputSession(
            target_id="term-1", generation_probe=lambda: self.generation, sender=self._send
        )
        extra: dict[str, Any] = {}
        if agent:
            extra = {
                "store": lambda: self.store,
                "allow_draft": True,
                "drops_engine_keys": lambda: self.modes.text_edits_draft,
            }
        self.input = KeyInput(
            KeyHooks(
                paused=lambda: self.paused,
                foreground=lambda: self.foreground,
                bound=lambda: self.bound,
                epoch=lambda: self.epoch,
                keys=lambda: self.session,
                modes=lambda: self.modes,
                notify=self._notify,
                **extra,
            )
        )

    async def _send(self, data: bytes) -> None:
        self.sent.append(data)

    def _notify(self) -> None:
        self.notified += 1

    def live_token(self) -> tuple[int, int]:
        token = self.input.input_token()
        assert token is not None
        return token


def run(coro: Any) -> Any:
    return asyncio.run(coro)


def test_everything_is_inert_without_a_bound_writer() -> None:
    async def go() -> None:
        rig = Rig()
        rig.bound = False
        assert await rig.input.type_text("ls") == ROUTE_DROPPED
        assert await rig.input.handle_view_input(ORIGIN_TEXT, b"ls") == ROUTE_DROPPED
        assert await rig.input.press_key("enter") is False
        assert await rig.input.flush() == 0
        assert rig.input.interrupt() is False
        rig.input.toggle_modifier("ctrl")
        assert rig.session.pending_modifiers == frozenset()
        assert rig.sent == [] and rig.notified == 0

    run(go())


@pytest.mark.parametrize("state", ["paused", "background", "unbound"])
def test_no_input_token_when_input_cannot_reach_a_writer(state: str) -> None:
    rig = Rig()
    assert rig.input.input_token() == (1, rig.session.token)
    if state == "paused":
        rig.paused = True
    elif state == "background":
        rig.foreground = False
    else:
        rig.bound = False
    assert rig.input.input_token() is None


def test_token_dies_with_any_writer_change() -> None:
    rig = Rig()
    token = rig.live_token()
    assert rig.input.token_live(token)
    rig.session.invalidate()  # unmount / selection change / takeover
    assert not rig.input.token_live(token)
    fresh = rig.live_token()
    assert rig.input.token_live(fresh)
    rig.epoch += 1  # rebind to a new writer
    assert not rig.input.token_live(fresh)
    token2 = rig.live_token()
    rig.paused = True
    assert not rig.input.token_live(token2)
    rig.paused = False
    rig.foreground = False
    assert not rig.input.token_live(token2)


def test_stale_token_input_is_dropped_and_nothing_is_sent() -> None:
    async def go() -> None:
        rig = Rig()
        stale = rig.live_token()
        rig.session.invalidate()
        assert await rig.input.handle_view_input(ORIGIN_KEY, b"\r", token=stale) == ROUTE_DROPPED
        assert await rig.input.handle_view_input(ORIGIN_TEXT, b"ls", token=stale) == ROUTE_DROPPED
        assert rig.sent == []
        # The same bytes with a live token do go out, exactly once.
        live = rig.live_token()
        assert await rig.input.handle_view_input(ORIGIN_KEY, b"\r", token=live) == ROUTE_PTY
        assert rig.sent == [b"\r"]

    run(go())


def test_view_input_is_dropped_while_paused_or_in_the_background() -> None:
    async def go() -> None:
        rig = Rig()
        rig.paused = True
        assert await rig.input.handle_view_input(ORIGIN_KEY, b"x") == ROUTE_DROPPED
        rig.paused, rig.foreground = False, False
        assert await rig.input.handle_view_input(ORIGIN_KEY, b"x") == ROUTE_DROPPED
        assert rig.sent == []

    run(go())


def test_mouse_reports_and_empty_data_never_become_input() -> None:
    async def go() -> None:
        rig = Rig()
        assert await rig.input.handle_view_input(ORIGIN_MOUSE, b"\x1b[M") == ROUTE_DROPPED
        assert await rig.input.handle_view_input(ORIGIN_KEY, b"") == ROUTE_DROPPED
        assert await rig.input.handle_view_input(ORIGIN_TEXT, b"") == ROUTE_DROPPED
        assert rig.sent == []

    run(go())


def test_engine_keys_and_text_travel_as_exact_bytes_through_the_one_writer() -> None:
    async def go() -> None:
        rig = Rig()
        assert await rig.input.handle_view_input(ORIGIN_KEY, b"\x1b[A") == ROUTE_PTY
        assert await rig.input.handle_view_input(ORIGIN_TEXT, "héllo".encode()) == ROUTE_PTY
        assert rig.sent == [b"\x1b[A", "héllo".encode()]
        assert rig.notified == 2

    run(go())


def test_a_queued_key_bound_to_an_old_generation_is_dropped_never_replayed() -> None:
    async def go() -> None:
        rig = Rig()
        rig.session.commit_bytes(b"rm -rf /tmp/x\r")  # queued for generation 1
        rig.generation = 2  # the writer was replaced before the flush
        assert await rig.input.flush() == 0
        assert rig.sent == []
        # The new writer never receives the old emission, now or later.
        assert await rig.input.flush() == 0 and rig.sent == []

    run(go())


def test_a_dead_writer_drops_the_queue_with_nothing_sent() -> None:
    async def go() -> None:
        rig = Rig()
        rig.session.commit_bytes(b"x")
        rig.generation = None
        assert await rig.input.flush() == 0 and rig.sent == []
        rig.generation = 1
        assert await rig.input.flush() == 0 and rig.sent == []

    run(go())


def test_press_key_sends_once_and_consumes_armed_modifiers() -> None:
    async def go() -> None:
        rig = Rig()
        rig.input.toggle_modifier("ctrl")
        assert rig.session.pending_modifiers == frozenset({"ctrl"})
        assert await rig.input.press_key("c") is True
        assert rig.sent == [b"\x03"] and rig.session.pending_modifiers == frozenset()

    run(go())


def test_unencodable_or_stale_press_sends_nothing_and_keeps_modifiers() -> None:
    async def go() -> None:
        rig = Rig()
        rig.input.toggle_modifier("ctrl")
        assert await rig.input.press_key("definitely-not-a-key") is False
        assert rig.sent == [] and rig.session.pending_modifiers == frozenset({"ctrl"})
        rig.generation = None  # dead writer
        assert await rig.input.press_key("c") is False
        assert rig.sent == [] and rig.session.pending_modifiers == frozenset({"ctrl"})

    run(go())


def test_interrupt_discards_pre_armed_modifiers_and_queues_ctrl_c() -> None:
    async def go() -> None:
        rig = Rig()
        rig.input.toggle_modifier("alt")
        assert rig.input.interrupt() is True
        assert rig.session.pending_modifiers == frozenset()
        assert await rig.input.flush() == 1 and rig.sent == [b"\x03"]

    run(go())


def test_composer_text_edits_the_draft_and_never_reaches_the_writer() -> None:
    async def go() -> None:
        rig = Rig(WORKFLOW_AGENT_COMPOSER, agent=True)
        assert await rig.input.type_text("hello") == ROUTE_DRAFT
        assert rig.store is not None and "hello" in rig.store.draft
        assert rig.sent == []

    run(go())


def test_composer_drops_engine_keys_but_explicit_tools_keys_still_reach_the_writer() -> None:
    async def go() -> None:
        rig = Rig(WORKFLOW_AGENT_COMPOSER, agent=True)
        assert await rig.input.handle_view_input(ORIGIN_KEY, b"\r") == ROUTE_DROPPED
        assert rig.sent == []
        assert await rig.input.press_key("enter") is True  # dock keys bypass the gate
        assert rig.sent == [b"\r"]

    run(go())


def test_direct_workflow_routes_text_to_the_pty_not_the_draft() -> None:
    async def go() -> None:
        rig = Rig(WORKFLOW_AGENT_DIRECT, agent=True)
        assert await rig.input.type_text("ls") == ROUTE_PTY
        assert rig.sent == [b"ls"]
        assert rig.store is not None and rig.store.draft == ""
        assert await rig.input.handle_view_input(ORIGIN_KEY, b"\r") == ROUTE_PTY

    run(go())


def test_a_shell_has_no_store_so_it_can_never_route_to_a_draft() -> None:
    async def go() -> None:
        rig = Rig(WORKFLOW_SHELL)
        assert await rig.input.type_text("ls") == ROUTE_PTY
        assert rig.sent == [b"ls"]

    run(go())
