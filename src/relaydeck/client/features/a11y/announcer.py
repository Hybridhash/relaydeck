"""Batched screen-reader announcements.

`StatusAnnouncer` is Flet-free with an injected millisecond clock:
stream churn collapses to at most ONE summary per aggregation window
(never per-token), while discrete significant status changes are each
announced once (consecutive duplicates collapse). `LiveRegion` is the
declarative `ft.Semantics` wrapper that surfaces announcements to
assistive technology (Flet 1.0.3 `Semantics.live_region`).
"""

from __future__ import annotations

from typing import Any

import flet as ft

ANNOUNCE_WINDOW_MS = 1000


class StatusAnnouncer:
    """One announcement per window, latest-wins with discrete priority.

    Time is quantized into windows `[k*W, (k+1)*W)`. Draining a window
    emits at most one string: the latest discrete status change when any
    arrived in that window, else a churn summary when stream output was
    noted. A discrete text identical to the last emitted announcement is
    dropped (no repeat). Windows with no input emit nothing.
    """

    def __init__(self, window_ms: int = ANNOUNCE_WINDOW_MS) -> None:
        self._window_ms = window_ms
        self._discrete: dict[int, str] = {}
        self._churn: dict[int, int] = {}
        self._last_emitted = ""

    def announce(self, text: str, now_ms: int) -> None:
        """Record a significant discrete status change (not stream churn)."""
        if not text:
            return
        window = now_ms // self._window_ms
        self._discrete[window] = text

    def note_output(self, now_ms: int, updates: int = 1) -> None:
        """Record stream churn (terminal bytes/tokens): batched, summarized."""
        if updates <= 0:
            return
        window = now_ms // self._window_ms
        self._churn[window] = self._churn.get(window, 0) + updates

    def drain(self, now_ms: int) -> tuple[str, ...]:
        """Emit at most one announcement per elapsed window, oldest first.

        Only windows strictly before the current window are drained; the
        live window keeps collecting so late arrivals still batch.
        """
        current = now_ms // self._window_ms
        windows = sorted(w for w in set(self._discrete) | set(self._churn) if w < current)
        emitted: list[str] = []
        for window in windows:
            text = self._discrete.pop(window, "")
            churn = self._churn.pop(window, 0)
            if text:
                if text != self._last_emitted:
                    emitted.append(text)
                    self._last_emitted = text
            elif churn:
                summary = f"Terminal output updated ({churn} updates)"
                emitted.append(summary)
                self._last_emitted = summary
        return tuple(emitted)


@ft.component
def LiveRegion(text: str, content: Any = None) -> Any:
    """A `live_region` Semantics node whose label carries the announcement.

    Pass the visible status control as `content` to keep it on screen
    while exposing `text` to assistive technology; otherwise a plain
    `ft.Text` is rendered.
    """
    return ft.Semantics(
        label=text,
        live_region=True,
        content=content if content is not None else ft.Text(text),
    )


__all__ = ["ANNOUNCE_WINDOW_MS", "LiveRegion", "StatusAnnouncer"]
