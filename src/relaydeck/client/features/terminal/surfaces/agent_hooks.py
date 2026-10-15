"""Render-state hooks called in a fixed order by the agent detail."""

from __future__ import annotations

import asyncio
import inspect
from collections.abc import Callable
from typing import Any

import flet as ft

from relaydeck.client.features.composer.domain import StagedMarker  # noqa: F401
from relaydeck.client.features.terminal.events import spawn
from relaydeck.client.features.terminal.input_modes import WORKFLOW_AGENT_COMPOSER
from relaydeck.client.features.terminal.layout_budget import measured_row_height


def use_row_metric(
    *, font_size: float | None, dock_open: bool
) -> tuple[
    tuple[int, int],
    Callable[[tuple[int, int]], Any],
    float,
    Callable[[float], Any],
    tuple[float | None, float | None],
]:
    """Window size, terminal host height and the measured px-per-row metric.

    (font size, px per row) is measured once per font from the host box and
    the engine's cols x rows report. Re-sampled when either input changes
    (so a stale pairing is corrected by the next report), but only
    replaced on a >8% difference so quantisation cannot feed back.
    """
    window_size, set_window_size = ft.use_state((0, 0))

    host_height, set_host_height = ft.use_state(0.0)
    initial_metric: tuple[float | None, float | None] = (None, None)
    row_metric, set_row_metric = ft.use_state(initial_metric)

    last_sample: Any = ft.use_ref(None)

    def sample_row_metric() -> None:
        # Sampled only while the dock is closed (the host box is then
        # independent of this budget) and locked per font after two
        # consecutive agreeing samples, so a host/report pair that is
        # momentarily out of step can neither be adopted nor feed back
        # into the layout (flicker).
        if dock_open or row_metric[0] == font_size and row_metric[1] is not None:
            return
        measured = measured_row_height(host_height, window_size[1])
        if measured is None:
            return
        previous = last_sample.current
        last_sample.current = measured
        if previous is not None and abs(measured - previous) <= 0.03 * previous:
            set_row_metric((font_size, measured))

    ft.use_effect(
        sample_row_metric,
        dependencies=[host_height, window_size, font_size, dock_open],
    )
    return window_size, set_window_size, host_height, set_host_height, row_metric


def use_staged_picture_reveal(
    *,
    pictures: tuple[Any, ...],
    draft_restore_marker: tuple[int, int],
    phone: bool,
    workflow: str,
    composer_focusable: bool,
    close_keyboard: Callable[..., Any],
    composing: Callable[[], Any],
) -> None:
    """Reveal newly staged picture chips on a phone without sending or focusing."""
    picture_ids = tuple(picture.picture_id for picture in pictures)
    previous_pictures: Any = ft.use_ref((picture_ids, draft_restore_marker))

    def reveal_staged_picture() -> None:
        previous_ids, previous_restore = previous_pictures.current
        added = set(picture_ids) - set(previous_ids)
        restored = previous_restore != draft_restore_marker
        # Always consume the baseline, including suppressed/restore events.
        # The completed report identifies restoration even if loading=True
        # and loading=False were coalesced into one declarative render.
        previous_pictures.current = (picture_ids, draft_restore_marker)
        if (
            not restored
            and phone
            and composer_focusable
            and workflow == WORKFLOW_AGENT_COMPOSER
            and added
        ):
            # Staging is draft-only. Reveal its chips without sending, focusing
            # the editor, or acquiring terminal control; use the pencil path.
            close_keyboard(composing)

    ft.use_effect(
        reveal_staged_picture,
        dependencies=[picture_ids, draft_restore_marker, phone, workflow, composer_focusable],
    )


OPEN_HOLD_S = 0.6
OPEN_SETTLE_S = 0.4
RESIZE_SETTLE_S = 0.15
RESIZE_MAX_WAIT_S = 0.5


def use_open_hold(
    expected: tuple[int, int] | None,
    forward: Callable[[int, int], Any],
    active: bool = True,
    *,
    settled: bool = True,
    input_phase: tuple[bool, bool] | None = None,
    binding: Any = None,
) -> tuple[Callable[[int, int], Any], bool]:
    """One bounded publisher for opening and subsequent geometry bursts.

    Matching the previous geometry is not evidence that the new layout is final.
    Native input/scroll enable can precede the final frame by 360 ms.
    Caps start only after readiness and cannot be postponed by reports or input changes.
    Live bursts settle briefly without hiding the surface. Each nonempty burst retains
    one refresh, even if it returns to the starting grid; reports also request repaint.
    """
    held, set_held = ft.use_state(True)
    holding: Any = ft.use_ref(True)
    pending: Any = ft.use_ref(None)
    timer: Any = ft.use_ref(None)
    fallback_timer: Any = ft.use_ref(None)
    previous: Any = ft.use_ref(None)
    status: Any = ft.use_ref((active, settled, binding))
    revision = ft.use_ref(0)
    if status.current != (active, settled, binding):
        revision.current += 1
        status.current = (active, settled, binding)
    captured = revision.current
    alive = ft.use_ref(True)
    sender: Any = ft.use_ref(None)
    queued: Any = ft.use_ref(None)
    hide_on_report: Any = ft.use_ref(False)
    forward_box: Any = ft.use_ref(None)  # a callable initial value would be called as a factory
    forward_box.current = forward

    def send(size: tuple[int, int], cap_due: bool) -> None:
        queued.current = (size, revision.current, forward_box.current, cap_due)
        if sender.current is not None:
            return

        async def drain() -> None:
            try:
                while sender.current is asyncio.current_task() and queued.current is not None:
                    latest, stamp, callback, _cap_due = queued.current
                    queued.current = None
                    if (
                        alive.current
                        and stamp == revision.current
                        and status.current[:2] == (True, True)
                    ):
                        result = callback(*latest)
                        if inspect.isawaitable(result):
                            await result
            finally:
                if sender.current is asyncio.current_task():
                    sender.current = None

        sender.current = spawn(drain(), label="terminal-resize")

    def release(stamp: int, cap_due: bool = False) -> None:
        if not alive.current or stamp != revision.current or status.current[:2] != (True, True):
            return
        disarm()
        if holding.current:
            set_held(False)
        holding.current = False
        latest, pending.current = pending.current, None
        if latest is not None and latest[0] == stamp:
            send(latest[1], cap_due)

    def disarm() -> None:
        for handle in (timer, fallback_timer):
            if handle.current is not None:
                handle.current.cancel()
                handle.current = None

    def arm(delay: float) -> None:
        if timer.current is not None:
            timer.current.cancel()
        timer.current = asyncio.get_running_loop().call_later(delay, release, revision.current)

    def arm_cap(delay: float) -> None:
        if fallback_timer.current is None:
            fallback_timer.current = asyncio.get_running_loop().call_later(
                delay, release, revision.current, True
            )

    def cancel_work() -> None:
        disarm()
        queued.current = None
        task, sender.current = sender.current, None
        if task is not None:
            task.cancel()

    def dispose() -> None:
        alive.current = False
        pending.current = None
        cancel_work()

    ft.use_effect(lambda: None, dependencies=[], cleanup=dispose)

    def quiet_delay() -> float:
        return min(OPEN_SETTLE_S, OPEN_HOLD_S) if expected is not None else OPEN_HOLD_S

    def follow_activity() -> None:
        before, previous.current = previous.current, (active, settled, binding)
        if not active:
            holding.current = False
            pending.current = None
            hide_on_report.current = False
            set_held(False)
            return
        if not settled:
            # Readiness loss invalidates geometry even while the cover is already held.
            pending.current = None
            queued.current = None
        if before is not None and (
            not before[0]
            or before[2] != binding
            or before[1]
            and not settled
            and not holding.current
        ):
            holding.current = True
            pending.current = None
            hide_on_report.current = True
        if settled and holding.current:
            # A continuous report stream may postpone quiet, never bounded progress.
            arm_cap(OPEN_HOLD_S)
            arm(quiet_delay() if pending.current is not None else OPEN_HOLD_S)

    ft.use_effect(follow_activity, dependencies=[active, settled, binding], cleanup=cancel_work)

    def follow_input_phase() -> None:
        # Enabling native input/scroll needs a fresh quiet interval, but
        # must neither postpone the cap nor re-arm a finished opening.
        if holding.current and status.current[:2] == (True, True) and pending.current is not None:
            arm(quiet_delay())

    ft.use_effect(follow_input_phase, dependencies=[input_phase])

    def report(cols: int, rows: int) -> Any:
        if not alive.current or captured != revision.current or not status.current[0]:
            return None
        if holding.current and hide_on_report.current:
            hide_on_report.current = False
            set_held(True)
        if not status.current[1]:
            return None
        waiting = queued.current
        if waiting is not None and waiting[1] == captured and waiting[3]:
            # A cap-expired publication stays due until this writer consumes it.
            # Replace its geometry, never erase it or restart the cap while it
            # waits for its first turn or for an in-flight resize to finish.
            queued.current = ((cols, rows), captured, forward_box.current, True)
            return None
        # Supersede a settled size still waiting behind an in-flight write.
        queued.current = None
        pending.current = (captured, (cols, rows))
        if holding.current:
            arm(quiet_delay())
        else:
            arm_cap(RESIZE_MAX_WAIT_S)
            arm(RESIZE_SETTLE_S)
        return None

    return report, held
