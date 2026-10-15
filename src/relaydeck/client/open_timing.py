"""Content-free timing of one terminal open, from the tap to a painted, input-ready terminal.

One record per open. Steps are marked once (the first time they happen) as milliseconds since
the tap, size reports and resize frames are counted, and a single fixed-schema line is logged when
the terminal is both painted (the phone acknowledged output) and input-ready, or when the open is
superseded or times out. Lines carry step names, numbers and enum-like flags only: never ids,
output, drafts or sizes' meaning beyond counts.

Steps reached through the opening task (gateway calls, stream socket, first bytes) find the record
through a context variable, which tasks created by that open inherit; steps driven by page events
(size reports, input verification, the phone's acknowledgement) find it on the session.
"""

from __future__ import annotations

import contextvars
import time
from collections.abc import Callable
from typing import Any

STEPS = (
    "acquired",
    "published",
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
    "verified",
    "input_ready",
    "first_ack",
    "painted",
    "first_resize",
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


class OpenTiming:
    def __init__(self, kind: str, clock: Callable[[], float] = time.monotonic) -> None:
        self.kind = kind if kind in ("agent", "shell") else "other"
        self.path = "cold"
        self.cache = False
        self._clock = clock
        self._t0 = clock()
        self._marks: dict[str, int] = {}
        self._counts = dict.fromkeys(COUNTS, 0)
        # (terminal identity, acknowledged bytes, post-frame reports) when this open first saw it
        self._base: tuple[int, int, int] | None = None
        self.done = False

    def elapsed_ms(self) -> int:
        return round((self._clock() - self._t0) * 1000)

    def mark(self, step: str) -> None:
        if self.done or step not in STEPS or step in self._marks:
            return
        if step == "attach":
            for control, view in _VIEW_STEPS.items():
                if control in self._marks:
                    self._marks.setdefault(view, self._marks.pop(control))
        self._marks[step] = self.elapsed_ms()
        # A resumed terminal already shows its screen: being input-ready completes it.
        painted = "painted" in self._marks or self.path in ("warm", "paused")
        if painted and "input_ready" in self._marks:
            self.finish("ready")

    def count(self, name: str) -> None:
        if not self.done and name in self._counts:
            self._counts[name] += 1

    def poll(self, term: Any, session: Any = None) -> None:
        """Called on the output watchdog's tick for the foreground terminal."""
        if self.done:
            return
        if session is not None and term is not None:
            # A fresh page runs no verification probe: the writer is ready once the session is
            # live, its controller unfenced and it holds an eligible control lease. The engine's
            # own input gate follows the workflow (the composer keeps it off), so it says nothing
            # about readiness.
            controller = getattr(session, "controller", None)
            if (
                getattr(session, "phase", "") == "live"
                and controller is not None
                and not getattr(controller, "ui_input_fenced", True)
                and getattr(controller, "writer_eligible", False)
            ):
                self.mark("input_ready")
        if term is not None:
            acked = getattr(term, "_acked_bytes", 0)
            frames = getattr(term, "_view_report_count", 0)
            # Only progress after this open counts: a retained terminal's earlier
            # acknowledgements and frames say nothing about this open.
            if self._base is None or self._base[0] != id(term):
                cold = self.path == "cold"
                self._base = (id(term), 0 if cold else acked, 0 if cold else frames)
            # The phone acknowledges output once its emulator applied it, before drawing it;
            # its post-frame view report follows the frame that shows it.
            if acked > self._base[1]:
                self.mark("first_ack")
            if "first_ack" in self._marks and frames > self._base[2]:
                self.mark("painted")
        if not self.done and self._clock() - self._t0 > TIMEOUT_S:
            self.finish("timeout")

    def finish(self, outcome: str) -> None:
        if self.done:
            return
        self.done = True
        # Imported here: the stream and gateway-call modules import this one, and the terminal
        # package that holds the logger imports them.
        from relaydeck.client.features.terminal.diagnostics import diagnostics_logger

        steps = " ".join(f"{step}={self._marks.get(step, '-')}" for step in STEPS)
        counts = " ".join(f"{name}={self._counts[name]}" for name in COUNTS)
        diagnostics_logger().info(
            "open_timing kind=%s path=%s cache=%d outcome=%s total_ms=%d %s %s",
            self.kind,
            self.path,
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


def mark_current(step: str) -> None:
    timing = _CURRENT.get()
    if timing is not None:
        timing.mark(step)


def of(session: Any) -> OpenTiming | None:
    timing = getattr(session, "open_timing", None)
    return timing if isinstance(timing, OpenTiming) and not timing.done else None
