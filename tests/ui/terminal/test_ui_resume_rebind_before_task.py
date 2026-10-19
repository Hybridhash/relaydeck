"""A resume cancelled before its task runs must not strand output.

The owner's reload left the probe cancelled (`why=lifecycle_changed`) before
`rebind_resumed` could move the terminal to the new transport, so every later
write went to the dead channel. `TerminalUIResumes.start` now points the terminal
at the live transport before scheduling, so the cancel cannot leave it behind.
Synthetic mux connections and a minimal fake session; no network.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any

from relaydeck_terminal import protocol

from relaydeck.client.features.terminal.ui_resume import TerminalUIResumes
from tests.support.host_harness import run
from tests.support.terminal.resume import _MuxConn, _open_term


class _Trace:
    def emit(self, *_args: Any, **_kwargs: Any) -> None:
        return None


class _FakeController:
    def __init__(self, term: Any) -> None:
        self.generation = 1
        self.lease_id = "lease-1"
        self.mode = "control"
        self.writer_revoked = False
        self.reattach_needed = False
        self.live_edge_pending = False
        self.pending_resume = None
        self.writer_eligible = True
        self.state = SimpleNamespace(epoch="epoch-1", status="live", notice=None)
        self._term = term

    def fence_ui_input(self, _fenced: bool) -> None:
        return None

    def on_viewer_overflow(self) -> None:
        return None


class _FakeSession:
    def __init__(self, term: Any) -> None:
        self.current_term = term
        self.controller = _FakeController(term)
        self.attachment = object()
        self.lease = SimpleNamespace(
            lease_id="lease-1", generation=1, mode="control", expires_at=None
        )
        self.disposed = False
        self.foreground = True
        self.paused = False
        self.interaction_generation = 0
        self.term_state = SimpleNamespace(epoch="epoch-1", selection=SimpleNamespace(open=False))
        self._renewal = None
        self._listeners: list[Any] = []

    def subscribe(self, listener: Any) -> Any:
        self._listeners.append(listener)
        return lambda: self._listeners.remove(listener)

    def _lease_trace(self, _lease: Any) -> _Trace:
        return _Trace()

    def bump_and_notify(self) -> None:
        """A lifecycle change (the owner's reload) during the attempt."""
        self.interaction_generation += 1
        for listener in tuple(self._listeners):
            listener(None)


def test_attempt_cancelled_before_its_task_runs_leaves_writes_on_the_live_channel() -> None:
    async def scenario() -> None:
        _app, term, _conn, old = await _open_term(7)
        term.write(b"before")

        # The page reloaded: the Flet transport is a new connection with a new channel.
        new_conn = _MuxConn()
        _app.session.attach_connection(new_conn)
        new = new_conn.data_channel_for(7)
        sent: list[bytes] = []
        new.send = sent.append  # type: ignore[method-assign]

        session = _FakeSession(term)
        resumes = TerminalUIResumes()
        task = resumes.start(
            session,
            transport=object(),
            current=lambda: True,
            closed=asyncio.Event(),
        )
        assert task is not None
        # The lifecycle bump cancels the attempt before its task has run.
        session.bump_and_notify()
        await asyncio.sleep(0)

        assert term._channel is new
        assert old.closed is True
        term.write(b"after")
        assert protocol.encode_write(b"after") in sent

    run(scenario())
