"""Selection actions must not cross writer, viewer or sheet replacement."""

from __future__ import annotations

import asyncio
from collections import deque
from dataclasses import replace
from typing import Any

import flet as ft
import pytest
from relaydeck_terminal import protocol
from relaydeck_terminal.terminal import RelayDeckTerminal

from relaydeck.client.features.terminal.surfaces import selection_actions
from tests.support.host_harness import HostApp, render_component, run
from tests.support.terminal.binding import _wait_for
from tests.support.terminal.channel_open import _HarnessChannel
from tests.support.terminal.sessions import _agent_session, _session


class _SnapshotTerm:
    def __init__(self) -> None:
        self.viewer_generation = 1
        self._reset_count = 0
        self.requests: deque[asyncio.Future[str]] = deque()
        self.started = asyncio.Event()
        self.clears = 0

    async def get_snapshot_text(self) -> str:
        self.started.set()
        if self.requests:
            return await self.requests.popleft()
        return "current synthetic text"

    def reset(self, *, reason: str = "") -> None:
        # Real reset retains the channel generation. Leave this delayed
        # source able to complete; real aborted waiters are covered below.
        self._reset_count += 1

    def clear_selection(self) -> None:
        self.clears += 1

    def set_input_enabled(self, enabled: bool) -> None:
        pass


async def _live(shell: bool) -> tuple[Any, _SnapshotTerm]:
    if shell:
        session, _ops, _factory = _session()
        await session.open("term-1")
    else:
        session, _ops, _factory, _submitted = _agent_session()
        session.open()
        await session.confirm_takeover()
    term = _SnapshotTerm()
    session.bind_view(term, write=lambda view, data: True)
    return session, term


async def _replace(session: Any, shell: bool) -> None:
    controller = session.controller
    await session.reconnect()
    if not shell:
        await session.confirm_takeover()
    assert session.controller is not None and session.controller is not controller


@ft.component
def _NativeResetHost() -> Any:
    return RelayDeckTerminal(key="selection-reset-term")


async def _live_native(shell: bool) -> tuple[Any, RelayDeckTerminal, _HarnessChannel, HostApp]:
    session, _old = await _live(shell)
    app = await render_component(_NativeResetHost)
    term = app.find("selection-reset-term")
    channel = _HarnessChannel()
    app.conn.data_channel_for = lambda channel_id: channel  # type: ignore[method-assign]
    await app.set_props_and_fire(
        "selection-reset-term",
        {},
        "data_channel_open",
        {"channel_name": "terminal", "channel_id": 7},
    )
    session.bind_view(term)
    assert term.channel_ready and term.viewer_generation == 1
    return session, term, channel, app


@pytest.mark.parametrize("shell", [False, True])
@pytest.mark.parametrize("completed", [False, True])
@pytest.mark.parametrize("recovered", [False, True])
def test_real_native_reset_invalidates_pending_or_queued_snapshot(
    shell: bool, completed: bool, recovered: bool
) -> None:
    async def scenario() -> None:
        session, term, channel, app = await _live_native(shell)
        pending = asyncio.create_task(selection_actions(session)[0]())
        try:
            await _wait_for(lambda: len(term._snapshot_waiters) == 1)
            assert channel.sent[-1][0] == protocol.REQUEST_SNAPSHOT
            request_id = next(iter(term._snapshot_waiters))
            if completed:
                # Parse an actual native response without yielding: completion
                # is queued when reset happens, so it must also be rejected.
                body = b"pre-reset synthetic snapshot"
                term._test_inject_dart_message(
                    protocol.encode_snapshot_chunk(request_id, len(body), 0, body)
                )
            controller = session.controller
            epoch = session.term_state.epoch
            view_token = session._view_token
            generation = controller.generation
            reset_count = term._reset_count
            controller._handle_reset("epoch_changed")
            assert term.viewer_generation == 1 and term._reset_count == reset_count + 1
            assert channel.sent[-1] == protocol.encode_reset()
            assert not term._snapshot_waiters
            assert session.term_state.status == "reset"
            if recovered:
                # A same-boundary live projection arrives before the queued
                # opener resumes. Transient reset status cannot be the fence.
                controller._set(replace(controller.state, status="live"))
            state, projected, text = (
                session.term_state,
                controller.state,
                session.selection_sheet_text,
            )
            await pending
            assert session.current_term is term and app.find("selection-reset-term") is term
            assert session.controller is controller and controller.generation == generation
            assert session._view_token == view_token and session.term_state.epoch == epoch
            assert session.term_state is state and controller.state is projected
            assert not state.selection.open and state.selection.error is None
            assert session.selection_sheet_text == text
        finally:
            if not pending.done():
                pending.cancel()
            await asyncio.gather(pending, return_exceptions=True)
            await session.dispose()

    run(scenario())


@pytest.mark.parametrize("shell", [False, True])
@pytest.mark.parametrize("failure", [False, True])
def test_delayed_snapshot_cannot_open_or_mirror_into_replacement_session(
    shell: bool, failure: bool
) -> None:
    async def scenario() -> None:
        session, term = await _live(shell)
        result: asyncio.Future[str] = asyncio.get_running_loop().create_future()
        term.requests.append(result)
        open_old, _move, _done = selection_actions(session)
        pending = asyncio.create_task(open_old())
        try:
            await asyncio.wait_for(term.started.wait(), 1)
            await _replace(session, shell)
            state, projected = session.term_state, session.controller.state
            text = session.selection_sheet_text
            if failure:
                result.set_exception(TimeoutError("old viewer failed"))
            else:
                result.set_result("old generation synthetic text")
            await pending
            assert session.term_state is state and not state.selection.open
            assert session.controller.state is projected
            assert session.selection_sheet_text == text
            # A current range event cannot turn an old completion into a sheet.
            selection_actions(session)[1](0, 3)
            assert session.term_state is state and session.controller.state is projected
        finally:
            if not result.done():
                result.set_result("ignored")
            await pending
            await session.dispose()

    run(scenario())


@pytest.mark.parametrize("shell", [False, True])
@pytest.mark.parametrize("replace_writer", [False, True])
def test_stale_move_done_and_open_leave_new_sheet_and_projection_unchanged(
    shell: bool, replace_writer: bool
) -> None:
    async def scenario() -> None:
        session, term = await _live(shell)
        try:
            old_open, old_move, old_done = selection_actions(session)
            await old_open()
            if replace_writer:
                await _replace(session, shell)
            else:
                selection_actions(session)[2]()
            new_open, new_move, new_done = selection_actions(session)
            await new_open()
            new_move(2, 7)
            state, projected, text = (
                session.term_state,
                session.controller.state,
                session.selection_sheet_text,
            )
            clears = term.clears
            old_move(0, 1)
            old_done()
            await old_open()
            assert session.term_state is state and session.controller.state is projected
            assert session.selection_sheet_text == text and term.clears == clears
            assert state.selection.open and (state.selection.anchor, state.selection.focus) == (
                2,
                7,
            )
            new_done()
            assert not session.term_state.selection.open
            assert session.controller.state.selection is session.term_state.selection
        finally:
            await session.dispose()

    run(scenario())


@pytest.mark.parametrize("shell", [False, True])
@pytest.mark.parametrize(
    "invalidate", ["done", "session-done", "viewer", "remount", "park", "dispose"]
)
def test_pending_snapshot_is_invalidated_before_commit(shell: bool, invalidate: str) -> None:
    async def scenario() -> None:
        session, term = await _live(shell)
        result: asyncio.Future[str] = asyncio.get_running_loop().create_future()
        term.requests.append(result)
        opener, _move, done = selection_actions(session)
        pending = asyncio.create_task(opener())
        try:
            await asyncio.wait_for(term.started.wait(), 1)
            if invalidate == "done":
                done()
            elif invalidate == "session-done":
                session.close_selection()
            elif invalidate == "viewer":
                term.reset(reason="stream_reset")
            elif invalidate == "remount":
                session.bind_view(term, write=lambda view, data: True)
            elif invalidate == "park":
                session.set_foreground(False)
                session.set_foreground(True)
            else:
                await session.dispose()
            state, text = session.term_state, session.selection_sheet_text
            result.set_result("dismissed synthetic text")
            await pending
            assert session.term_state is state and session.selection_sheet_text == text
            assert not session.term_state.selection.open
        finally:
            if not result.done():
                result.set_result("ignored")
            await pending
            await session.dispose()

    run(scenario())


@pytest.mark.parametrize("shell", [False, True])
def test_latest_snapshot_request_wins_and_ordinary_output_preserves_sheet(shell: bool) -> None:
    async def scenario() -> None:
        session, term = await _live(shell)
        old: asyncio.Future[str] = asyncio.get_running_loop().create_future()
        new: asyncio.Future[str] = asyncio.get_running_loop().create_future()
        term.requests.extend((old, new))
        opener, move, done = selection_actions(session)
        pending_old = asyncio.create_task(opener())
        pending_new: asyncio.Task[None] | None = None
        try:
            await asyncio.wait_for(term.started.wait(), 1)
            term.started.clear()
            pending_new = asyncio.create_task(opener())
            await asyncio.wait_for(term.started.wait(), 1)
            new.set_result("latest synthetic snapshot")
            await pending_new
            move(0, 6)
            state = session.term_state
            # The same selection object survives normal controller projection.
            projected = replace(session.controller.state, applied_offset=42, notice="more output")
            session.controller.state = projected
            session.update_term_state(projected)
            old.set_result("older synthetic snapshot")
            await pending_old
            assert session.selection_sheet_text == "latest synthetic snapshot"
            assert session.term_state is projected and projected.selection is state.selection
            assert (projected.selection.anchor, projected.selection.focus) == (0, 6)
            done()
            assert not session.controller.state.selection.open
            assert (
                session.term_state.applied_offset == 42
                and session.term_state.notice == "more output"
            )
        finally:
            for result in (old, new):
                if not result.done():
                    result.set_result("ignored")
            await pending_old
            if pending_new is not None:
                await pending_new
            await session.dispose()

    run(scenario())
