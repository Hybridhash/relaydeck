"""alt/no-mouse swipe origin routes to bounded pages, never arrows.

Dispatched through the real agent/shell screens: swipe INPUT packets must
translate to PageUp/PageDown on the PTY (never arrow bytes, never typed
payload text), honor watch/selection/mode fences, and leave drafts alone.
"""

from dataclasses import replace

import pytest
from relaydeck_terminal import protocol

from tests.support.host_harness import run
from tests.support.terminal.latch_text import _inputs
from tests.support.terminal.scroll_ui import _app, _view

PAGE_UP = b"\x1b[5~"
PAGE_DOWN = b"\x1b[6~"
ARROWS = (b"\x1b[A", b"\x1b[B", b"\x1bOA", b"\x1bOB")


async def _alt_no_mouse(shell):
    app, session, socket, channel = await _app(shell, "compact")
    await _view(app, channel, alt=True, bottom=False, mouse=False)
    return app, session, socket, channel


@pytest.mark.parametrize("shell", [False, True])
@pytest.mark.parametrize(
    "direction,page",
    [(protocol.SWIPE_UP, PAGE_UP), (protocol.SWIPE_DOWN, PAGE_DOWN)],
)
def test_swipe_direction_routes_to_single_page(shell, direction, page):
    async def scenario():
        app, session, socket, channel = await _alt_no_mouse(shell)
        try:
            before = len(_inputs(socket))
            channel.handler(protocol.encode_swipe(direction, 23))
            await app.settle()
            fresh = _inputs(socket)[before:]
            assert fresh == [page]
            assert not any(chunk in ARROWS for chunk in fresh)
            if not shell:
                assert session.store.draft == "retained compose draft"
        finally:
            await session.dispose()

    run(scenario())


@pytest.mark.parametrize("shell", [False, True])
def test_swipe_pages_bounded_per_packet(shell):
    async def scenario():
        app, session, socket, channel = await _alt_no_mouse(shell)
        try:
            before = len(_inputs(socket))
            channel.handler(protocol.encode_swipe(protocol.SWIPE_DOWN, 1))
            await app.settle()
            assert _inputs(socket)[before:] == [PAGE_DOWN]
            before = len(_inputs(socket))
            channel.handler(protocol.encode_swipe(protocol.SWIPE_DOWN, 255))
            await app.settle()
            fresh = _inputs(socket)[before:]
            assert 1 <= len(fresh) <= protocol.MAX_SWIPE_PAGES
            assert all(chunk == PAGE_DOWN for chunk in fresh)
        finally:
            await session.dispose()

    run(scenario())


@pytest.mark.parametrize("shell", [False, True])
def test_swipe_never_emits_arrows_across_counts(shell):
    async def scenario():
        app, session, socket, channel = await _alt_no_mouse(shell)
        try:
            for count in list(range(1, 256, 37)) + [255]:
                for direction, page in (
                    (protocol.SWIPE_UP, PAGE_UP),
                    (protocol.SWIPE_DOWN, PAGE_DOWN),
                ):
                    before = len(_inputs(socket))
                    channel.handler(protocol.encode_swipe(direction, count))
                    await app.settle()
                    fresh = _inputs(socket)[before:]
                    assert 1 <= len(fresh) <= protocol.MAX_SWIPE_PAGES
                    assert all(chunk == page for chunk in fresh)
                    assert not any(chunk in ARROWS for chunk in fresh)
        finally:
            await session.dispose()

    run(scenario())


@pytest.mark.parametrize("shell", [False, True])
@pytest.mark.parametrize("bad", [b"", b"\x01", b"\x01\x02\x03", b"\x02\x05", b"\x01\x00"])
def test_malformed_swipe_is_dropped_not_typed(shell, bad):
    async def scenario():
        app, session, socket, channel = await _alt_no_mouse(shell)
        try:
            before = len(_inputs(socket))
            channel.handler(protocol.encode_input(bad, protocol.ORIGIN_SWIPE))
            await app.settle()
            assert len(_inputs(socket)) == before
            if not shell:
                assert session.store.draft == "retained compose draft"
        finally:
            await session.dispose()

    run(scenario())


@pytest.mark.parametrize("shell", [False, True])
@pytest.mark.parametrize("alt,mouse", [(False, False), (True, True), (False, True)])
def test_swipe_out_of_mode_is_dropped(shell, alt, mouse):
    async def scenario():
        app, session, socket, channel = await _app(shell, "compact")
        try:
            await _view(app, channel, alt=alt, bottom=False, mouse=mouse)
            before = len(_inputs(socket))
            channel.handler(protocol.encode_swipe(protocol.SWIPE_DOWN, 23))
            await app.settle()
            assert len(_inputs(socket)) == before
            if not shell:
                assert session.store.draft == "retained compose draft"
        finally:
            await session.dispose()

    run(scenario())


@pytest.mark.parametrize("shell", [False, True])
def test_swipe_honors_watch_and_selection_fences(shell):
    async def scenario():
        app, session, socket, channel = await _alt_no_mouse(shell)
        try:
            before = len(_inputs(socket))
            closed_selection = session.term_state.selection
            session._term_state = replace(
                session.term_state, selection=replace(closed_selection, open=True)
            )
            session._notify()
            await app.settle()
            channel.handler(protocol.encode_swipe(protocol.SWIPE_DOWN, 23))
            await app.settle()
            assert len(_inputs(socket)) == before
            session._term_state = replace(session.term_state, selection=closed_selection)
            session.controller.mode = "view"
            session.controller.state = replace(session.controller.state, lease_mode="view")
            session._term_state = replace(session.term_state, lease_mode="view")
            session._notify()
            await app.settle()
            channel.handler(protocol.encode_swipe(protocol.SWIPE_DOWN, 23))
            await app.settle()
            assert len(_inputs(socket)) == before
        finally:
            await session.dispose()

    run(scenario())


@pytest.mark.parametrize("shell", [False, True])
@pytest.mark.parametrize("arrow", [b"\x1b[A", b"\x1b[B", b"\x1bOA", b"\x1bOB"])
def test_physical_arrows_still_flow_in_alt_no_mouse(shell, arrow):
    async def scenario():
        app, session, socket, channel = await _alt_no_mouse(shell)
        try:
            before = len(_inputs(socket))
            channel.handler(protocol.encode_input(arrow, protocol.ORIGIN_KEY))
            await app.settle()
            assert _inputs(socket)[before:] == [arrow]
        finally:
            await session.dispose()

    run(scenario())
