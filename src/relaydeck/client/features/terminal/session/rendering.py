"""One renderer binding per terminal session.

Owns output sinks, viewer-reopen detection and explicit recovery. Optional
callbacks handle workflow-specific steps without a session back-reference."""

from __future__ import annotations

import asyncio
import logging
import time
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from relaydeck_terminal.diagnostics import trace_lifecycle

from relaydeck.client.features.terminal.controller import TerminalController
from relaydeck.lease_diagnostics import LeaseTrace

DEFAULT_REOPEN_NOTICE = "Viewer restarted; reattach explicitly. History may be incomplete."
CONTINUITY_NOTICE = (
    "Terminal continuity unavailable; reattach explicitly. History may be incomplete."
)


def _noop() -> None:
    return None


# At most RESET_LIMIT renderer resets per RESET_WINDOW_S seconds per viewer.
RESET_WINDOW_S = 10.0
RESET_LIMIT = 5


@dataclass(frozen=True)
class RendererHooks:
    """What the binding needs from its session (no session back-reference)."""

    session_id: int  # only for lifecycle trace correlation
    controller: Callable[[], TerminalController | None]
    clear_controller: Callable[[], None]
    foreground: Callable[[], bool]
    disposed: Callable[[], bool]
    exited: Callable[[], bool]
    sessions_bound: Callable[[], bool]
    bind_sessions: Callable[[TerminalController], None]
    invalidate_sessions: Callable[[], str | None]
    restart_renewal: Callable[[], None]
    cancel_renewal: Callable[[], None]
    bump_interaction: Callable[[str], None]
    lease_trace: Callable[[], LeaseTrace]
    notify: Callable[[], None]
    mark_disconnected: Callable[[str], None]  # phase -> disconnected, notice
    note_stale_notice: Callable[[str | None], None]  # notice only while live and unset
    # Agent-only extras; shells leave them as no-ops.
    cancel_watch: Callable[[], None] = _noop
    bump_epoch: Callable[[], None] = _noop
    detach_store: Callable[[], None] = _noop


class RendererBinding:
    def __init__(self, hooks: RendererHooks) -> None:
        self._hooks = hooks
        self._sinks: dict[int, Callable[[bytes], None]] = {}
        self._terms: dict[int, Any] = {}
        self._seq = 0
        self._view_token: int | None = None
        self._removers: dict[int, Callable[[], None]] = {}
        self._viewer_binding: tuple[Any, int] | None = None
        self._close_tasks: set[asyncio.Task[None]] = set()
        # A per-viewer cap on renderer resets, so a reset that keeps coming back
        # backs off and stops instead of looping.
        self._reset_marks: deque[float] = deque()
        self._reset_capped = False

    # -- observable state -------------------------------------------------
    @property
    def view_token(self) -> int | None:
        return self._view_token

    @property
    def viewer_binding(self) -> tuple[Any, int] | None:
        return self._viewer_binding

    @property
    def current_term(self) -> Any | None:
        """The bound stable renderer, or None when no view is mounted."""
        if self._view_token is None:
            return None
        return self._terms.get(self._view_token)

    def terms(self) -> list[Any]:
        return list(self._terms.values())

    # -- sinks --------------------------------------------------------------
    def bind_renderer(self, sink: Callable[[bytes], None]) -> int:
        """Register an output sink; returns its registration token."""
        self._seq += 1
        self._sinks[self._seq] = sink
        return self._seq

    def unbind_renderer(self, token: int) -> None:
        self._sinks.pop(token, None)
        self._terms.pop(token, None)
        remove = self._removers.pop(token, None)
        if remove is not None:
            remove()

    def is_sink_live(self, token: int) -> bool:
        return token in self._sinks

    def deliver(self, data: bytes) -> None:
        """Feed every live sink; a dead renderer must not kill the stream."""
        for sink in list(self._sinks.values()):
            try:
                sink(data)
            except Exception:  # noqa: S110 - a dead renderer must not kill the stream
                pass

    # -- view mount ---------------------------------------------------------
    def bind_view(self, term: Any, *, write: Callable[[Any, bytes], bool] | None = None) -> int:
        """Mount a renderer view: rebind input sessions to the live
        controller (dropping stale callbacks), register the output sink,
        and restart lease renewal. Replaces any previous mount: one
        mounted renderer per session. Returns the sink token. `write`
        defaults to `screen.deliver_output` (lazy import keeps this
        module Flet-free)."""
        hooks = self._hooks
        if write is None:
            from relaydeck.client.features.terminal.screen import deliver_output

            write = deliver_output
        if self._view_token is not None:
            self.unbind_renderer(self._view_token)
            self._view_token = None
        controller = hooks.controller()
        # Bind only when unbound (fresh mount or remount after unbind):
        # controller replacement already retargets through the attach
        # paths, so a bound session is never stale here: retargeting
        # would drop a legitimate open review and queued input.
        if (
            controller is not None
            and hooks.foreground()
            and not hooks.disposed()
            and not hooks.sessions_bound()
        ):
            hooks.bind_sessions(controller)
        slot: dict[str, int] = {}

        def _sink(data: bytes) -> None:
            if not self.is_sink_live(slot["token"]):
                return
            if not write(term, data):
                active = hooks.controller()
                if active is not None:
                    active.on_viewer_overflow()
                hooks.notify()

        token = self.bind_renderer(_sink)
        slot["token"] = token
        self._terms[token] = term
        self._view_token = token
        logging.getLogger("relaydeck.client.typing_split").info(
            "term_bind term=%s session=%s gen=%s token=%s",
            id(term) % 100000,
            hooks.session_id % 100000,
            getattr(term, "viewer_generation", 0),
            token % 100000,
        )
        subscribe = getattr(term, "add_channel_listener", None)
        if callable(subscribe):

            def reopened() -> None:
                if self.is_sink_live(token):
                    previous = self._viewer_binding
                    self._viewer_binding = (term, term.viewer_generation)
                    if previous is not None and (
                        previous[0] is not term or previous[1] != term.viewer_generation
                    ):
                        hooks.lease_trace().emit(
                            "viewer_reopen",
                            reason="reopened",
                            source="native",
                            viewer=term.viewer_generation,
                        )
                        term.fence_fresh_viewer()
                        self.viewer_reopened()
                    hooks.notify()  # Also reconcile a first channel opened after UI reconnect.

            self._removers[token] = subscribe(reopened)
            if term.viewer_generation > 0:
                reopened()
            hooks.lease_trace().emit(
                "viewer_bind", reason="bound", source="native", viewer=term.viewer_generation
            )
            trace_lifecycle(
                "viewer_bound",
                session=hooks.session_id,
                viewer=id(term),
                generation=term.viewer_generation,
                binding=token,
            )
        hooks.restart_renewal()
        hooks.notify()
        return token

    def unbind_view(self) -> None:
        """Unmount the renderer view: drop the output sink, invalidate all
        input callbacks (queued input, modifiers, tokens, open reviews:
        nothing is sent), and cancel lease renewal. A later `bind_view`
        rebinds the live writer; no uncertain input is replayed."""
        hooks = self._hooks
        logging.getLogger("relaydeck.client.typing_split").info(
            "term_unbind term=%s session=%s gen=%s token=%s",
            id(self.current_term) % 100000 if self.current_term is not None else 0,
            hooks.session_id % 100000,
            getattr(self.current_term, "viewer_generation", 0),
            self._view_token % 100000 if self._view_token is not None else 0,
        )
        hooks.bump_interaction("unbind_view")
        hooks.cancel_watch()
        if self._view_token is not None:
            self.unbind_renderer(self._view_token)
            self._view_token = None
        hooks.cancel_renewal()
        stale = hooks.invalidate_sessions()
        hooks.bump_epoch()
        hooks.note_stale_notice(stale)
        hooks.notify()

    # -- explicit recovery --------------------------------------------------
    def prepare_ui_remount(self) -> None:
        """Retire the renderer, preserving the lease/draft for output recovery."""
        self.unbind_view()
        self._viewer_binding = None

    def require_ui_recovery(self) -> None:
        """Unverified transport continuity: explicit recovery, never automatic attach/reset."""
        self.viewer_reopened(CONTINUITY_NOTICE)

    def viewer_reopened(self, notice: str = DEFAULT_REOPEN_NOTICE) -> None:
        hooks = self._hooks
        controller = hooks.controller()
        if controller is None or hooks.disposed() or hooks.exited():
            return
        hooks.mark_disconnected(notice)
        hooks.bump_interaction("viewer_reopened")
        hooks.cancel_watch()
        hooks.invalidate_sessions()
        hooks.bump_epoch()
        hooks.detach_store()
        hooks.cancel_renewal()
        controller.viewer_reopened()
        hooks.clear_controller()
        task = asyncio.create_task(controller.close())
        self._close_tasks.add(task)

        def finished(done: asyncio.Task[None]) -> None:
            self._close_tasks.discard(done)
            if not done.cancelled():
                done.exception()

        task.add_done_callback(finished)
        hooks.notify()

    def reset_bound_renderers(self, reason: str = "controller_replaced") -> None:
        now = time.monotonic()
        marks = self._reset_marks
        while marks and now - marks[0] > RESET_WINDOW_S:
            marks.popleft()
        if len(marks) >= RESET_LIMIT:
            if not self._reset_capped:
                self._reset_capped = True
                logging.getLogger("relaydeck.client.terminal").warning(
                    "terminal_reset_capped reason=%s window_s=%s limit=%s",
                    reason,
                    int(RESET_WINDOW_S),
                    RESET_LIMIT,
                )
            return
        self._reset_capped = False
        marks.append(now)
        for term in list(self._terms.values()):
            reset = getattr(term, "reset", None)
            if callable(reset):
                try:
                    if hasattr(term, "viewer_generation"):
                        reset(reason=reason)
                    else:
                        reset()
                except Exception:  # noqa: S110 - a dead renderer must not kill the stream
                    pass

    async def reset_history(self, current: Callable[[], bool]) -> bool:
        """Reset the captured native parser and verify its acknowledgement without PTY I/O."""
        terms = tuple(self._terms.items())
        for token, term in terms:
            if term is None or not current() or self._terms.get(token) is not term:
                return False
            rebind = getattr(term, "rebind_resumed", None)
            if not callable(rebind):
                return False  # An unacknowledged native reset never enables input.
            viewer = term.viewer_generation
            term.set_input_enabled(False)
            term.reset(reason="stream_reset")
            resets = term._reset_count

            def valid(
                token: int = token, term: Any = term, viewer: int = viewer, resets: int = resets
            ) -> bool:
                return (
                    term is not None
                    and current()
                    and term.viewer_generation == viewer
                    and term._reset_count == resets
                    and self._terms.get(token) is term
                )

            outcome = await rebind(timeout=1.5, current=valid)
            if not valid() or outcome.status != "resumed":
                return False
        return current()

    async def drain_close_tasks(self) -> None:
        await asyncio.gather(*self._close_tasks, return_exceptions=True)
