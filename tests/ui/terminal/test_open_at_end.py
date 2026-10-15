"""A terminal brought to the front shows its newest output.

The engine stops following the end once a touch moves its view, so a terminal reopened from the
list could show an older part of its history until the reader pressed the jump button. Bringing
a terminal to the front now sends that same jump.
"""

from __future__ import annotations

from typing import Any

import flet as ft
from relaydeck_terminal.protocol import ViewState

from relaydeck.client.features.terminal.scrolling import _views, use_open_at_end
from tests.support.host_harness import render_component, run


class _Term:
    def __init__(self, shown: bool = True) -> None:
        self.jumps: list[int | None] = []
        if shown:
            _views[self] = (None, ViewState(alt=False, at_bottom=False, mouse=False))

    def scroll_viewport(self, pages: int | None) -> None:
        self.jumps.append(pages)


class _Session:
    def __init__(self, term: _Term | None) -> None:
        self.current_term = term
        self.foreground = True
        self.view_jumps = 0

    def jump_to_bottom(self) -> None:
        self.view_jumps += 1


def _probe(session: _Session, out: dict[str, Any]):
    @ft.component
    def Probe() -> ft.Control:
        _tick, set_tick = ft.use_state(0)
        out["rerender"] = lambda: set_tick(lambda value: value + 1)
        use_open_at_end(session)
        return ft.Text("probe", key="probe")

    return Probe


def test_each_return_to_the_front_jumps_to_the_end() -> None:
    async def scenario() -> None:
        term = _Term()
        session = _Session(term)
        out: dict[str, Any] = {}
        app = await render_component(_probe(session, out))
        assert term.jumps == [None] and session.view_jumps == 1
        out["rerender"]()  # an ordinary re-render (new output, a resize) never moves the view
        await app.settle()
        assert term.jumps == [None]
        session.foreground = False
        out["rerender"]()
        await app.settle()
        assert term.jumps == [None]
        session.foreground = True
        out["rerender"]()
        await app.settle()
        assert term.jumps == [None, None] and session.view_jumps == 2

    run(scenario())


def test_a_replaced_engine_jumps_only_once_it_has_been_shown() -> None:
    async def scenario() -> None:
        session = _Session(_Term())
        out: dict[str, Any] = {}
        app = await render_component(_probe(session, out))
        fresh = _Term(shown=False)  # a new engine follows the end on its own
        session.current_term = fresh
        out["rerender"]()
        await app.settle()
        assert fresh.jumps == []
        shown = _Term()
        session.current_term = shown
        out["rerender"]()
        await app.settle()
        assert shown.jumps == [None]

    run(scenario())


def test_the_alternate_screen_and_a_missing_engine_are_left_alone() -> None:
    async def scenario() -> None:
        term = _Term()
        _views[term] = (None, ViewState(alt=True, at_bottom=True, mouse=False))  # full-screen app
        out: dict[str, Any] = {}
        await render_component(_probe(_Session(term), out))
        assert term.jumps == []
        empty = _Session(None)
        await render_component(_probe(empty, {}))
        assert empty.view_jumps == 0

    run(scenario())


def test_a_view_report_that_arrives_after_opening_still_brings_it_to_the_end() -> None:
    async def scenario() -> None:
        term = _Term(shown=False)
        session = _Session(term)
        out: dict[str, Any] = {}
        app = await render_component(_probe(session, out))
        assert term.jumps == []
        _views[term] = (None, ViewState(alt=False, at_bottom=False, mouse=False))
        out["rerender"]()
        await app.settle()
        assert term.jumps == [None]
        out["rerender"]()  # later renders leave a deliberate scroll alone
        await app.settle()
        assert term.jumps == [None]

    run(scenario())


def test_a_reset_does_not_turn_the_next_deliberate_scroll_into_a_jump() -> None:
    async def scenario() -> None:
        term = _Term()
        term._reset_count = 0  # type: ignore[attr-defined]
        _views[term] = (0, ViewState(alt=False, at_bottom=True, mouse=False))
        session = _Session(term)
        out: dict[str, Any] = {}
        app = await render_component(_probe(session, out))
        assert term.jumps == [None]
        term._reset_count = 1  # type: ignore[attr-defined]  # resync; unchanged bits send no report
        out["rerender"]()
        await app.settle()
        _views[term] = (1, ViewState(alt=False, at_bottom=False, mouse=False))  # the user scrolled
        out["rerender"]()
        await app.settle()
        out["rerender"]()
        await app.settle()
        assert term.jumps == [None]

    run(scenario())
