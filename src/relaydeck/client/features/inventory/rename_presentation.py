"""One sheet presentation at a time: only its own dismissal is user intent.

A native sheet or dialog closes only when its `open` goes true to false; removing the control
leaves it on screen. So every close first renders `open=False` and unmounts once the client
reports the close (or after a short fallback).
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

import flet as ft

CLOSE_FALLBACK_S = 0.5


@dataclass(frozen=True)
class Gate:
    closing: bool
    request: Callable[[], None]
    reported: Callable[[], None]


class _GateState:
    def __init__(self) -> None:
        self.closing = False
        self.generation = 0
        self.task: Any = None
        self.count = 0
        self.shown = False


def use_close_gate(
    shown: bool, on_close: Callable[[], Any], generation: int = 0, fallback: float | None = None
) -> Gate:
    """Close a native overlay in two steps: render it closed, then call `on_close`.

    `request` closes at once when the overlay is not shown. `reported` is the client's close
    report; the fallback covers a report that never comes.
    """
    state: Any = ft.use_ref(_GateState)
    gate: _GateState = state.current
    _tick, bump = ft.use_state(0)
    gate.shown = shown
    if gate.generation != generation:
        gate.generation = generation
        gate.closing = False

    def cancel() -> None:
        if gate.task is not None:
            gate.task.cancel()
            gate.task = None

    ft.use_effect(lambda: None, dependencies=[], cleanup=cancel)
    seen = generation

    def finish() -> None:
        cancel()
        if seen == gate.generation and gate.closing:
            on_close()

    def request() -> None:
        if not gate.shown:
            on_close()
            return
        if gate.closing:
            return
        gate.closing = True
        gate.count += 1
        bump(gate.count)

        async def later() -> None:
            await asyncio.sleep(CLOSE_FALLBACK_S if fallback is None else fallback)
            gate.task = None
            finish()

        gate.task = asyncio.create_task(later(), name="relaydeck-close-gate")

    return Gate(gate.closing, request, finish)


@dataclass(frozen=True)
class Presentation:
    open: bool
    generation: int
    dismissed: Callable[[], None]
    close: Callable[[], None]


class _State:
    def __init__(self) -> None:
        self.visible = False
        self.shown_before = False
        self.generation = 0


def use_presentation(wanted: bool, on_close: Callable[[], Any]) -> Presentation:
    """Track whether the sheet is shown and which presentation is current.

    Showing the sheet again after it was hidden mounts a new presentation (its own subtree, so
    its own native controls). The client's close report for the earlier hide targets the earlier,
    removed control and is dropped by Flet, however late it arrives. A dismissal from the current
    presentation while it is shown is the user closing it; one while it is hidden by us is only
    the report of that hide.
    """
    state: Any = ft.use_ref(_State)
    current: _State = state.current
    if wanted and not current.visible and current.shown_before:
        current.generation += 1
    current.visible = wanted
    current.shown_before = current.shown_before or wanted
    generation = current.generation
    gate = use_close_gate(wanted, on_close, generation)

    def dismissed() -> None:
        if generation != current.generation:
            return
        if gate.closing:
            gate.reported()
        elif current.visible:
            on_close()

    return Presentation(wanted and not gate.closing, generation, dismissed, gate.request)
