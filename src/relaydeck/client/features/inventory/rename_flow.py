"""State of the rename flow around the agent list: menu, sheet, optimistic names, toast."""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Collection, Sequence
from dataclasses import dataclass, replace
from functools import partial
from typing import Any

import flet as ft

from relaydeck.client.components.design_system.toast import ActionToast
from relaydeck.client.features.inventory.agent_gestures import AgentMenu
from relaydeck.client.features.inventory.rename import RenameDialog
from relaydeck.client.features.inventory.rename_wiring import (
    Overrides,
    sheet_context,
    taken_names,
)
from relaydeck.client.features.inventory.selectors import AgentRow
from relaydeck.domain.models import AppState

HIGHLIGHT_SECONDS = 1.2


@dataclass(frozen=True)
class RenameRun:
    """One rename being made. `preset` renames without showing the sheet (Undo, Retry)."""

    key: str
    initial: str
    nonce: int
    preset: str | None = None
    silent: bool = False


@dataclass(frozen=True)
class ToastModel:
    kind: str  # "done" | "error"
    key: str
    name: str
    previous: str
    closed: bool = False


class RenameFlow:
    def __init__(
        self,
        overrides: Overrides,
        highlight: str,
        run: RenameRun | None,
        menu: tuple[str, int],
        toast: ToastModel | None,
        actions: dict[str, Callable[..., Any]],
        leaving: bool = False,
    ) -> None:
        self.leaving = leaving
        self.overrides = overrides
        self.highlight = highlight
        self.run = run
        self.menu, self._menu_seq = menu
        self.toast = toast
        self._actions = actions

    def open_rename(self, key: str, title: str) -> None:
        self._actions["open"](key, title)

    def set_menu(self, key: str) -> None:
        self._actions["menu"](key)

    def overlays(
        self,
        *,
        rows: Sequence[AgentRow],
        state: AppState | None,
        transport: Any,
        on_select: Callable[[str], Any],
        on_refresh: Callable[[], Any] | None,
        dark: bool,
        phone: bool,
        foreground: bool,
        pins: Collection[str] = (),
        on_pin: Callable[[str], Any] | None = None,
    ) -> list[Any]:
        by_key = {row.key: row for row in rows}
        out: list[Any] = []
        if self.menu:
            row = by_key.get(self.menu)
            out.append(
                AgentMenu(  # type: ignore[call-arg]
                    key=f"agent-menu-host-{self.menu}-{self._menu_seq}",
                    agent_key=self.menu,
                    on_rename=lambda: self.open_rename(row.key, row.title) if row else None,
                    on_open=lambda: on_select(row.key) if row else None,
                    on_close=partial(self._actions["menu_closed"], self._menu_seq),
                    dark=dark,
                    gone=row is None,
                    pinned=self.menu in pins,
                    on_pin=(lambda: on_pin(self.menu)) if on_pin is not None and row else None,
                )
            )
        run = self.run
        showing = run is not None and state is not None and transport is not None
        self._actions["mounted"](showing)
        if run is not None and state is not None and transport is not None:
            found = by_key.get(run.key)
            out.append(
                RenameDialog(  # type: ignore[call-arg]
                    key=f"agent-rename-form-{run.key}-{run.nonce}",
                    agent_key=run.key,
                    initial_name=run.initial,
                    state=state,
                    transport=transport,
                    on_close=self._actions["close"],
                    on_refresh=on_refresh,
                    dark=dark,
                    foreground=foreground,
                    context=sheet_context(found, state, dark) if found else None,
                    taken=taken_names(found, rows, state) if found else (),
                    phone=phone,
                    preset=run.preset,
                    leaving=self.leaving,
                    on_pending=self._actions["pending"](run),
                    on_result=self._actions["result"](run),
                    on_abandon=self._actions["abandon"](run),
                )
            )
        else:
            out.append(ft.Container(key="agent-rename-slot"))
        if self.toast is not None:
            out.append(self._toast(self.toast))
        return out

    def _toast(self, toast: ToastModel) -> Any:
        if toast.kind == "done":
            return ActionToast(
                f"agent-rename-toast-{toast.key}",
                "Renamed to ",
                "Undo",
                lambda: self._actions["undo"](toast),
                self._actions["dismiss_toast"],
                emphasis=toast.name,
                open=not toast.closed,
            )
        return ActionToast(
            f"agent-rename-toast-{toast.key}",
            "Couldn't rename — try again",
            "Retry",
            lambda: self._actions["retry"](toast),
            self._actions["dismiss_toast"],
            error=True,
            open=not toast.closed,
        )


def use_rename_flow(rows: Sequence[AgentRow]) -> RenameFlow:
    no_run: RenameRun | None = None
    no_toast: ToastModel | None = None
    run, set_run = ft.use_state(no_run)
    shown_run: Any = ft.use_ref(None)
    shown_run.current = run
    queued: Any = ft.use_ref(None)
    mounted: Any = ft.use_ref(False)
    leaving, set_leaving = ft.use_state(0)
    menu, set_menu = ft.use_state(("", 0))
    menu_ref: Any = ft.use_ref(("", 0))
    toast, set_toast = ft.use_state(no_toast)
    shown: Any = ft.use_ref(None)
    shown.current = toast
    highlight, set_highlight = ft.use_state("")
    store: Any = ft.use_ref(dict)
    _revision, bump = ft.use_state(0)
    version: Any = ft.use_ref(0)
    counter: Any = ft.use_ref(0)
    latest: Any = ft.use_ref(None)
    latest.current = rows
    timer: Any = ft.use_ref(None)
    # An override ends when the authoritative title moves off the one it replaced (resolved or
    # superseded) or its agent is gone; it never revives when a later title repeats the old one.
    titles = {row.key: row.title for row in rows}
    for key in [k for k, entry in store.current.items() if titles.get(k) != entry[1]]:
        del store.current[key]

    def touch() -> None:
        version.current += 1
        bump(version.current)

    def base_title(key: str) -> str:
        return next((row.title for row in latest.current if row.key == key), "")

    def close_toast() -> None:
        # Closed first, never removed while open: the native snack bar outlives its control.
        current = shown.current
        if current is not None and not current.closed:
            set_toast(replace(current, closed=True))

    def start(key: str, initial: str, preset: str | None = None, silent: bool = False) -> None:
        counter.current += 1
        close_toast()
        attempt = RenameRun(key, initial, counter.current, preset, silent)
        if shown_run.current is None or not mounted.current:
            set_run(attempt)
        else:
            # The sheet on screen closes natively first; the next run takes over then.
            queued.current = attempt
            set_leaving(counter.current)

    def finish_run() -> None:
        following, queued.current = queued.current, None
        set_leaving(0)
        set_run(following)

    def pending(attempt: RenameRun) -> Callable[[str], None]:
        def mark(name: str) -> None:
            store.current[attempt.key] = (name, base_title(attempt.key), attempt.nonce)
            set_highlight(attempt.key)
            touch()

        return mark

    def result(attempt: RenameRun) -> Callable[[str, bool], None]:
        def settle(name: str, ok: bool) -> None:
            latest_attempt = counter.current == attempt.nonce
            if not ok:
                drop(attempt)
                if latest_attempt:
                    set_highlight("")
                    set_toast(ToastModel("error", attempt.key, name, attempt.initial))
            elif latest_attempt:
                set_toast(
                    None
                    if attempt.silent
                    else ToastModel("done", attempt.key, name, attempt.initial)
                )
            touch()

        return settle

    def drop(attempt: RenameRun) -> None:
        entry = store.current.get(attempt.key)
        if entry is not None and entry[2] == attempt.nonce:
            del store.current[attempt.key]

    def abandon(attempt: RenameRun) -> Callable[[str], None]:
        def gone(_name: str) -> None:
            drop(attempt)
            touch()

        return gone

    def undo(model: ToastModel) -> None:
        start(model.key, model.name, preset=model.previous, silent=True)

    def retry(model: ToastModel) -> None:
        start(model.key, model.previous, preset=model.name)

    def clear_highlight() -> None:
        if not highlight:
            return

        async def later() -> None:
            await asyncio.sleep(HIGHLIGHT_SECONDS)
            set_highlight("")

        timer.current = asyncio.create_task(later(), name="relaydeck-rename-highlight")

    def stop_timer() -> None:
        if timer.current is not None:
            timer.current.cancel()
            timer.current = None

    ft.use_effect(clear_highlight, dependencies=[highlight], cleanup=stop_timer)

    def show_menu(key: str) -> None:
        # Each opening is its own menu, so a closing one never clears a newer opening.
        menu_ref.current = (key, menu_ref.current[1] + 1)
        set_menu(menu_ref.current)

    def menu_closed(seq: int) -> None:
        if menu_ref.current[1] == seq:
            show_menu("")

    actions: dict[str, Callable[..., Any]] = {
        "open": start,
        "menu": show_menu,
        "menu_closed": menu_closed,
        "close": finish_run,
        "mounted": lambda value: setattr(mounted, "current", value),
        "pending": pending,
        "result": result,
        "abandon": abandon,
        "undo": undo,
        "retry": retry,
        "dismiss_toast": close_toast,
    }
    return RenameFlow(store.current, highlight, run, menu, toast, actions, leaving > 0)
