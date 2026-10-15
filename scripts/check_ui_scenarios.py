"""Headless UI scenario checks.

Builds the fake-adapter worlds, drives AppState through
the real reducers, asserts selector/banner expectations, and render-
smokes the inventory shell on the host harness. Not a browser test:
the browser matrix stays a manual release-gate item.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
from typing import Any

os.environ["RELAYDECK_FIXTURES"] = "1"

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from relaydeck.client.adapters.fake import FakeClipboard  # noqa: E402
from relaydeck.client.features.inventory.components import AppShell  # noqa: E402
from relaydeck.client.features.inventory.selectors import (  # noqa: E402
    agent_rows,
    connection_banner,
    host_rows,
    shell_rows,
)
from relaydeck.client.features.terminal.controller import TerminalController  # noqa: E402
from relaydeck.client.features.terminal.models import (  # noqa: E402
    TerminalViewState,
    apply_bytes,
    apply_gap,
    apply_live,
    apply_reset,
    jump_to_bottom,
    user_scroll_away,
)
from relaydeck.client.features.terminal.projection import TerminalProjection  # noqa: E402
from relaydeck.client.features.terminal.screen import TerminalScreen  # noqa: E402
from relaydeck.client.features.terminal.selection_model import (  # noqa: E402
    SelectionRange,
    close_selection,
    copy_range,
    document_from_projection,
    normalize_range,
    open_selection,
    range_for_lines,
)
from relaydeck.client.features.terminal.selection_sheet import SelectTextSheet  # noqa: E402
from relaydeck.client.features.terminal.stream import (  # noqa: E402
    ACTION_CONTINUE,
    ACTION_REATTACH,
    ACTION_RESUME,
    EMULATOR_FRESH,
    EMULATOR_PRESERVED,
    INCOMPLETE_HISTORY,
    StreamEvent,
    TerminalByteStream,
    decide_recovery,
)
from relaydeck.devtools.scenarios import build_scenario  # noqa: E402
from relaydeck.domain import reducers  # noqa: E402
from relaydeck.domain.models import AppState, ConnectionState  # noqa: E402
from relaydeck.gateway import terminal_wire  # noqa: E402
from tests.support.host_harness import render_component  # noqa: E402

FAILURES: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    status = "PASS" if cond else "FAIL"
    print(f"[{status}] {name}" + (f": {detail}" if detail and not cond else ""))
    if not cond:
        FAILURES.append(name)


def _noop(*args: Any) -> None:
    pass


async def _renders(state: AppState, destination: str) -> bool:
    try:
        app = await render_component(
            AppShell,
            state=state,
            is_wide=True,
            destination=destination,
            on_destination=_noop,
            on_select_agent=_noop,
            on_select_shell=_noop,
            on_select_host=_noop,
            on_banner_action=_noop,
            on_open_terminal=_noop,
            on_start_agent=_noop,
            on_new_terminal=_noop,
        )
        return bool(app.find("destination-title").value)
    except Exception as exc:  # noqa: BLE001 - report as check failure
        print(f"  render error: {exc}")
        return False


def _drive(name: str) -> AppState:
    # host-offline syncs while up, then drops: last known rows stay.
    world = build_scenario("steady" if name == "host-offline" else name)
    state = AppState()
    snapshot = world.host.snapshot()
    state = reducers.apply_snapshot(state, snapshot)
    _, events = world.host.poll("")
    for event in events:
        state, _ = reducers.apply_event(state, event)
    if name == "host-offline":
        state = reducers.mark_offline(state)
    else:
        state = reducers.set_connection(state, ConnectionState.LIVE)
    return state


def check_inventory() -> None:
    steady = _drive("steady")
    check("steady: agents listed", len(agent_rows(steady)) >= 3)
    check("steady: shells listed", len(shell_rows(steady)) >= 2)
    check("steady: hosts listed", len(host_rows(steady)) >= 1)
    check("steady: no banner", connection_banner(steady) is None)
    rows = agent_rows(steady)
    order = [r.status.value for r in rows]
    check(
        "steady: focused first, blocked before done",
        rows[0].focused and order.index("blocked") < order.index("done"),
        str(order),
    )

    offline = _drive("host-offline")
    banner = connection_banner(offline)
    check("host-offline: banner shown", banner is not None)
    check(
        "host-offline: banner warns",
        banner is not None and "offline" in banner.text.lower(),
        banner.text if banner else "no banner",
    )
    check("host-offline: rows retained", len(agent_rows(offline)) >= 3)

    dupes = _drive("duplicates")
    once = _drive("steady")
    check(
        "duplicates: same agents as steady",
        {r.key for r in agent_rows(dupes)} == {r.key for r in agent_rows(once)},
    )
    check("duplicates: no resync forced", dupes.needs_resync is False)

    async def renders() -> None:
        for destination in ("agents", "terminals", "hosts", "settings"):
            check(
                f"render: {destination}",
                await _renders(steady, destination),
            )
        check("render: offline agents", await _renders(offline, "agents"))

    asyncio.run(renders())


def check_terminal() -> None:
    vt = os.path.join(os.path.dirname(__file__), "..", "tests", "fixtures", "vt")
    with open(os.path.join(vt, "01_ascii_colors.bin"), "rb") as fh:
        colors = fh.read()
    with open(os.path.join(vt, "09_split_sequences.bin"), "rb") as fh:
        split = fh.read()

    # Ordered fixtures through the byte stream into the headless oracle.
    stream = TerminalByteStream()
    projection = TerminalProjection()
    for offset, data in ((0, colors), (len(colors), split)):
        event = stream.push(terminal_wire.OutputChunk("e1", offset, data))
        check(f"terminal: chunk @{offset} ordered", event.kind == "data")
        projection.feed(event.data)
    projection.flush()
    check(
        "terminal: fixtures render",
        projection.lines[:2] == ["plain red normal", "second line"],
        str(projection.lines[:2]),
    )
    check("terminal: offsets advance", stream.next_offset == len(colors) + len(split))

    # Split multibyte/escape delivery renders identically.
    want = TerminalProjection()
    want.feed(split)
    want.flush()
    for cut in (0, 7, 12, len(split)):
        candidate = TerminalProjection()
        candidate.feed(split[:cut])
        candidate.feed(split[cut:])
        candidate.flush()
        check(f"terminal: split cut {cut} identical", candidate.lines == want.lines)

    # Gap and reset decisions with a preserved emulator.
    gap = stream.push(terminal_wire.OutputChunk("e1", stream.next_offset + 50, b"x"))
    check("terminal: offset skip is a gap", gap.kind == "gap" and gap.data == b"")
    resume = decide_recovery(gap, stream, EMULATOR_PRESERVED)
    check(
        "terminal: preserved gap resumes",
        resume.action == ACTION_RESUME
        and resume.resume is not None
        and resume.resume.offset == stream.next_offset,
    )
    reset = stream.push(terminal_wire.OutputChunk("e2", 0, b"new"))
    check("terminal: epoch change is a reset", reset.kind == "reset" and reset.data == b"")
    reattach = decide_recovery(reset, stream, EMULATOR_PRESERVED)
    check(
        "terminal: reset reattaches with notice",
        reattach.action == ACTION_REATTACH and reattach.notice == INCOMPLETE_HISTORY,
    )
    fresh_tail = decide_recovery(
        StreamEvent("data", 500, b"tail"), stream, EMULATOR_FRESH, start_offset=500
    )
    check(
        "terminal: mid-stream start labeled incomplete",
        fresh_tail.action == ACTION_CONTINUE and fresh_tail.notice == INCOMPLETE_HISTORY,
    )

    # View-state reducers: scroll preservation plus the jump affordance.
    state = TerminalViewState(lease_mode="control")
    state = apply_live(state, "e1", 0)
    state = user_scroll_away(state)
    state = apply_bytes(state, 2048)
    check("terminal: scroll preserved on output", state.show_jump_to_latest is True)
    check("terminal: unseen bytes accrue", state.scroll.unseen_bytes == 2048)
    state = jump_to_bottom(state)
    check(
        "terminal: jump re-sticks",
        state.show_jump_to_latest is False and state.scroll.unseen_bytes == 0,
    )
    state = apply_gap(state, "offset 10 -> 60")
    check("terminal: gap state reconnecting", state.status == "reconnecting")
    state = apply_reset(state, INCOMPLETE_HISTORY)
    check(
        "terminal: reset notice shown",
        state.status == "reset" and state.notice is not None,
    )

    # Controller: view leases never emit resize frames.
    controller = TerminalController(
        socket_factory=lambda ticket: None,  # type: ignore[return-value]
        lease_id="lease-1",
        generation=1,
        mode="view",
        on_bytes=lambda data: None,
    )
    check("terminal: view resize filtered", controller.wants_resize(100, 30) is None)
    check("terminal: view rejection recorded", controller.rejected_resizes == [(100, 30)])

    # Headless render: surface, notice, jump affordance, status.
    async def renders() -> None:
        shown = apply_reset(TerminalViewState(lease_mode="view"), INCOMPLETE_HISTORY)
        shown = user_scroll_away(shown)
        app = await render_component(
            TerminalScreen,
            state=shown,
            on_jump_to_bottom=_noop,
            on_size_report=_noop,
            on_input=_noop,
        )
        try:
            app.find("terminal-surface")
            check("terminal: surface renders", True)
        except KeyError:
            check("terminal: surface renders", False)
        try:
            notice = app.find("terminal-notice-text")
            check("terminal: notice renders", notice.value == INCOMPLETE_HISTORY)
        except KeyError:
            check("terminal: notice renders", False)
        try:
            jump = app.find("scroll-jump")
            check("terminal: jump affordance renders", "Jump to latest" in str(jump.content))
        except KeyError:
            check("terminal: jump affordance renders", False)
        try:
            status = app.find("terminal-status")
            check("terminal: status renders", "reset" in str(status.value))
        except KeyError:
            check("terminal: status renders", False)

    asyncio.run(renders())


def check_selection() -> None:
    vt = os.path.join(os.path.dirname(__file__), "..", "tests", "fixtures", "vt")

    def feed(name: str, cols: int = 80, rows: int = 24) -> TerminalProjection:
        with open(os.path.join(vt, name), "rb") as fh:
            data = fh.read()
        projection = TerminalProjection(cols=cols, rows=rows)
        projection.feed(data)
        projection.flush()
        return projection

    # Scrollback + screen form one selectable document.
    doc = document_from_projection(feed("08_scrollback.bin"))
    check("selection: scrollback+screen line count", doc.line_count == 30, str(doc.line_count))
    check(
        "selection: boundary range exact",
        range_for_lines(doc, 6, 8).extract(doc.text) == "L07\nL08\nL09",
    )

    # Unicode fixtures copy exactly.
    wide = document_from_projection(feed("05_wide_emoji.bin"))
    check("selection: wide/emoji text exact", wide.text == "AB中文C\n😀 done", repr(wide.text))
    combining = document_from_projection(feed("06_combining.bin"))
    check("selection: combining text exact", combining.text == "e\u0301clair")
    check(
        "selection: combining mark never splits",
        SelectionRange(0, 1).extract(combining.text) == "e\u0301",
    )

    # Long wrapped lines join without phantom newlines.
    wrapped = TerminalProjection(cols=10, rows=24)
    wrapped.feed(b"0123456789ABCDEF\r\nshort")
    wrapped.flush()
    wrapped_doc = document_from_projection(wrapped)
    check(
        "selection: wrapped line has no phantom newline",
        wrapped_doc.text == "0123456789ABCDEF\nshort",
        repr(wrapped_doc.text),
    )

    # A missing or out-of-bounds anchor selects everything.
    check(
        "selection: missing anchor selects all",
        normalize_range(None, None, wide.text).extract(wide.text) == wide.text,
    )
    check(
        "selection: out-of-bounds anchor selects all",
        normalize_range(999, 1, wide.text).extract(wide.text) == wide.text,
    )
    check(
        "selection: reversed anchor/focus equivalent",
        SelectionRange(8, 2).extract(wide.text) == SelectionRange(2, 8).extract(wide.text),
    )

    # Explicit copy through the clipboard port is byte-exact.
    async def flows() -> None:
        clipboard = FakeClipboard()
        selection = range_for_lines(wide, 0, 1)
        written = await copy_range(wide.text, selection, clipboard)
        check("selection: copy returns exact text", written == wide.text)
        check("selection: clipboard write byte-exact", clipboard.writes == [wide.text])
        check("selection: copy never reads clipboard", clipboard.reads == 0)

        # Sheet renders anchored, read-only, monospaced; copy/close are explicit.
        copied: list[tuple[int, int]] = []
        closed: list[bool] = []
        app = await render_component(
            SelectTextSheet,
            text=wide.text,
            anchor=0,
            focus=len(wide.text),
            on_copy=lambda a, f: copied.append((a, f)),
            on_close=lambda: closed.append(True),
        )
        try:
            title = app.find("select-text-title")
            check("selection: sheet title", title.value == "Select Text")
        except KeyError:
            check("selection: sheet title", False)
        try:
            field = app.find("select-text-field")
            check(
                "selection: field read-only mono",
                field.read_only is True
                and field.text_style.font_family == "monospace"
                and field.value == wide.text,
            )
        except KeyError:
            check("selection: field read-only mono", False)
        await app.set_props_and_fire("select-text-copy", {}, "click")
        check("selection: copy callback anchored", copied == [(0, len(wide.text))], str(copied))
        await app.set_props_and_fire("select-text-close", {}, "click")
        check("selection: close callback", closed == [True])

        # Entering/exiting selection emits no PTY input.
        inputs: list[tuple[int, bytes]] = []
        state = open_selection(TerminalViewState(lease_mode="control"))
        screen = await render_component(
            TerminalScreen,
            state=state,
            on_jump_to_bottom=_noop,
            on_size_report=_noop,
            on_input=lambda origin, data: inputs.append((origin, data)),
            on_open_selection=_noop,
            selection_text=wide.text,
            on_copy_selection=lambda a, f: None,
            on_close_selection=lambda: None,
        )
        await screen.set_props_and_fire("select-text-copy", {}, "click")
        await screen.set_props_and_fire("select-text-close", {}, "click")
        check("selection: no PTY input emitted", inputs == [], str(inputs))
        check("selection: close clears open", close_selection(state).selection.open is False)

    asyncio.run(flows())


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description="Headless UI scenario checks.")
    parser.add_argument("--scenario", required=True, help="inventory, terminal, selection")
    parser.add_argument("--list", action="store_true", help="list scenarios")
    args = parser.parse_args(argv)
    if args.list or args.scenario == "list":
        print("inventory")
        print("terminal")
        print("selection")
        return 0
    if args.scenario == "inventory":
        check_inventory()
    elif args.scenario == "terminal":
        check_terminal()
    elif args.scenario == "selection":
        check_selection()
    else:
        print(f"scenario {args.scenario!r} is not implemented for UI checks")
        return 2
    print(f"{len(FAILURES)} failures")
    return 1 if FAILURES else 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
