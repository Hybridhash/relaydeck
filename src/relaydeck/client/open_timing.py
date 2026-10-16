"""Content-free timing from a terminal tap to paint, settled geometry and an eligible writer.

One record per open. Steps are marked once (the first time they happen) as milliseconds since
the tap, size reports and resize frames are counted, and a single fixed-schema line is logged when
the terminal is fully ready, or when the open is superseded or times out. Lines carry step names,
numbers and enum-like flags only: never ids,
output, drafts or sizes' meaning beyond counts.

Steps reached through the opening task (gateway calls, stream socket, first bytes) find the record
through a context variable, which tasks created by that open inherit; steps driven by page events
(size reports, input verification, the phone's acknowledgement) find it on the session.
"""

from __future__ import annotations

import contextvars
import time
import weakref
from collections.abc import Callable
from typing import Any

STEPS = (
    "acquired",
    "published",
    "hold_armed",
    "view_lease",
    "view_ticket",
    "view_socket",
    "view_first_byte",
    "disclosure",
    "confirm",
    "attach",
    "lease",
    "ticket",
    "socket",
    "first_byte",
    "probe_start",
    "probe_sent",
    "verified",
    "probe_verified",
    "unfenced",
    "input_ready",
    "first_output",
    "first_ack",
    "painted",
    "first_resize",
    "hold_released",
    "fully_ready",
)
COUNTS = ("size_reports", "resizes", "probe_cancels")
# Stream steps seen before the control attach belong to the view connection that kept the
# terminal warm; the attach starts the control connection's own steps.
_VIEW_STEPS = {"ticket": "view_ticket", "socket": "view_socket", "first_byte": "view_first_byte"}
TIMEOUT_S = 20.0

_CURRENT: contextvars.ContextVar[OpenTiming | None] = contextvars.ContextVar(
    "relaydeck_open_timing", default=None
)
_latest: OpenTiming | None = None
# One passive listener per live renderer. Neither it nor this table retains an open record.
_observed: weakref.WeakKeyDictionary[Any, weakref.ReferenceType[OpenTiming]] = (
    weakref.WeakKeyDictionary()
)


class OpenTiming:
    def __init__(self, kind: str, clock: Callable[[], float] = time.monotonic) -> None:
        self.kind = kind if kind in ("agent", "shell") else "other"
        self.path = "cold"
        self.cache = False
        self._clock = clock
        self._t0 = clock()
        self._marks: dict[str, int] = {}
        self._counts = dict.fromkeys(COUNTS, 0)
        self._term: Any = None
        self._identity: tuple[int, int, int] | None = None
        self._base_acked = 0
        self._paint_acked = 0
        self._paint_valid = False
        self._hold_released = False
        self.done = False

    def elapsed_ms(self) -> int:
        return round((self._clock() - self._t0) * 1000)

    def mark(self, step: str) -> None:
        if self.done or step not in STEPS:
            return
        if step in ("hold_armed", "hold_released"):
            self._hold_released = step == "hold_released"
        if step in self._marks:
            return
        if step == "attach":
            for control, view in _VIEW_STEPS.items():
                if control in self._marks:
                    self._marks.setdefault(view, self._marks.pop(control))
        self._marks[step] = self.elapsed_ms()

    def count(self, name: str) -> None:
        if not self.done and name in self._counts:
            self._counts[name] += 1

    @staticmethod
    def _renderer_identity(term: Any) -> tuple[int, int, int]:
        return (id(term), getattr(term, "viewer_generation", 0), getattr(term, "_reset_count", 0))

    def observe(self, term: Any) -> None:
        """Capture the open/bind boundary before output or another frame is requested."""
        if self.done:
            return
        if term is None:
            self._term = self._identity = None
            self._paint_valid = False
            return
        identity = self._renderer_identity(term)
        if identity == self._identity:
            return
        # A reset/reopen invalidates paint even when it retains the Python control. ACKs after
        # a reset start from zero; a different renderer may already contain retained output.
        same_term = term is self._term
        self._term, self._identity = term, identity
        self._base_acked = 0 if same_term else getattr(term, "_acked_bytes", 0)
        self._paint_valid = False
        self._paint_acked = 0
        if term not in _observed:
            target = weakref.ref(term)

            def frame(_: Any) -> None:
                renderer = target()
                record = _observed.get(renderer) if renderer is not None else None
                timing = record() if record is not None else None
                if timing is not None and not timing.done and timing._term is renderer:
                    timing._frame(renderer)

            term.add_view_state_listener(frame)
        _observed[term] = weakref.ref(self)

    def _ack(self, term: Any) -> int:
        acked = getattr(term, "_acked_bytes", 0)
        if acked > self._base_acked:
            self.mark("first_output")
            self.mark("first_ack")
        return acked

    def _frame(self, term: Any) -> None:
        # This callback runs at VIEW_REPORT dispatch, so it proves the report followed ACK.
        # Sampling two cumulative counters later cannot establish that ordering.
        self.observe(term)
        acked = self._ack(term)
        if acked > 0:
            self._paint_valid = True
            self._paint_acked = acked
            self.mark("painted")
        if self.kind == "shell" and "hold_armed" not in self._marks:
            self.mark("hold_released")

    def poll(self, term: Any, session: Any = None) -> None:
        """Called on the output watchdog's tick for the foreground terminal."""
        if self.done:
            return
        self.observe(term)
        ready = session is None and "input_ready" in self._marks and "unfenced" in self._marks
        if session is not None and term is not None:
            # Composer input gating does not indicate control-writer readiness.
            controller = getattr(session, "controller", None)
            if controller is not None and not getattr(controller, "ui_input_fenced", True):
                self.mark("unfenced")
            ready = (
                getattr(session, "phase", "") == "live"
                and controller is not None
                and not getattr(controller, "ui_input_fenced", True)
                and getattr(controller, "writer_eligible", False)
            )
            if ready:
                self.mark("input_ready")
        if term is not None:
            if self._ack(term) > self._paint_acked:
                self._paint_valid = False
        if ready and self._hold_released and self._paint_valid:
            self.mark("fully_ready")
            self.finish("ready")
        if not self.done and self._clock() - self._t0 > TIMEOUT_S:
            self.finish("timeout")

    def finish(self, outcome: str) -> None:
        if self.done:
            return
        self.done = True
        self._term = None
        # Imported here: the stream and gateway-call modules import this one, and the terminal
        # package that holds the logger imports them.
        from relaydeck.client.features.terminal.diagnostics import diagnostics_logger

        steps = " ".join(f"{step}={self._marks.get(step, '-')}" for step in STEPS)
        counts = " ".join(f"{name}={self._counts[name]}" for name in COUNTS)
        diagnostics_logger().info(
            "open_timing kind=%s path=%s cache=%d outcome=%s total_ms=%d %s %s",
            self.kind,
            self.path if self.path in ("cold", "warm", "paused") else "cold",
            int(self.cache),
            outcome if outcome in ("ready", "timeout", "superseded", "failed") else "other",
            self.elapsed_ms(),
            steps,
            counts,
        )


def begin(kind: str) -> OpenTiming:
    """A new open: the previous one, if still running, is reported as superseded."""
    global _latest
    if _latest is not None:
        _latest.finish("superseded")
    _latest = OpenTiming(kind)
    return _latest


def bind(timing: OpenTiming | None) -> None:
    """Make `timing` the record for this task and the tasks it creates."""
    if timing is not None and not timing.done:
        _CURRENT.set(timing)


def mark_current(step: str, *, session: Any = None) -> None:
    timing = of(session) if session is not None else _CURRENT.get()
    if timing is not None:
        timing.mark(step)


def of(session: Any) -> OpenTiming | None:
    timing = getattr(session, "open_timing", None)
    return timing if isinstance(timing, OpenTiming) and not timing.done else None
