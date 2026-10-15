"""Output selection and copy.

Domain checks run against the projection oracle as the
selection source; sheet and screen checks render on the real Flet 1.0.3
session via the host harness (no Flutter, no browser). Real-device
clipboard proof on web/Android stays a release-gate item.
"""

from __future__ import annotations

import ast
from dataclasses import replace
from typing import Any

from relaydeck_terminal.protocol import CLEAR_SELECTION, REQUEST_SELECTION_TEXT
from relaydeck_terminal.terminal import RelayDeckTerminal

from relaydeck.client.adapters.fake import FakeClipboard
from relaydeck.client.adapters.ports import ClipboardPort
from relaydeck.client.features.terminal.models import (
    SelectionState,
    TerminalViewState,
    apply_live,
)
from relaydeck.client.features.terminal.projection import TerminalProjection
from relaydeck.client.features.terminal.screen import TerminalScreen
from relaydeck.client.features.terminal.selection_model import (
    SelectionRange,
    clear_live_selection,
    close_selection,
    copy_all,
    copy_live_selection,
    copy_range,
    copy_text,
    document_from_projection,
    document_from_rows,
    mark_copied,
    move_anchor,
    move_focus,
    normalize_range,
    offset_at,
    open_selection,
    range_for_lines,
    resolved_range,
    set_selection_error,
    str_to_utf16_offset,
    utf16_to_str_offset,
)
from relaydeck.client.features.terminal.selection_sheet import (
    SelectTextSheet,
    range_label,
)
from tests.support.host_harness import HostApp, button_label, render_component, run

VT = "tests/fixtures/vt"


def _feed(data: bytes, cols: int = 80, rows: int = 24) -> TerminalProjection:
    projection = TerminalProjection(cols=cols, rows=rows)
    projection.feed(data)
    projection.flush()
    return projection


def _read(name: str) -> bytes:
    with open(f"{VT}/{name}", "rb") as fh:
        return fh.read()


# -- document building over the projection oracle ---------------------------


def test_multiline_range_copy_exact() -> None:
    projection = _feed(b"line1\r\nline2\r\nline3")
    doc = document_from_projection(projection)
    assert doc.logical_lines == ("line1", "line2", "line3")
    assert doc.text == "line1\nline2\nline3"
    selection = range_for_lines(doc, 1, 2)
    assert selection.extract(doc.text) == "line2\nline3"
    first = range_for_lines(doc, 0, 0)
    assert first.extract(doc.text) == "line1"
    assert offset_at(doc, 2, 4) == len("line1\nline2\nline")


def test_unicode_copy_exact() -> None:
    wide = document_from_projection(_feed(_read("05_wide_emoji.bin")))
    assert wide.text == "AB中文C\n😀 done"
    combining = document_from_projection(_feed(_read("06_combining.bin")))
    assert combining.text == "e\u0301clair"
    # Wide/emoji offsets are code-point safe: slicing never corrupts bytes.
    assert wide.text.encode("utf-8") == "AB中文C\n😀 done".encode()
    whole = SelectionRange(0, len(wide.text))
    assert whole.extract(wide.text).encode("utf-8") == wide.text.encode("utf-8")


def test_combining_mark_never_splits() -> None:
    doc = document_from_projection(_feed(_read("06_combining.bin")))
    # Offset 1 lands between 'e' and the combining acute: the edge snaps
    # out to keep the grapheme whole.
    assert SelectionRange(1, 3).extract(doc.text) == "e\u0301clair"[:3]
    assert SelectionRange(0, 1).extract(doc.text) == "e\u0301"
    assert SelectionRange(1, 1).extract(doc.text) == "e\u0301"


def test_long_wrapped_line_has_no_phantom_newline() -> None:
    projection = _feed(b"0123456789ABCDEF\r\nshort", cols=10)
    assert projection.lines == ["0123456789", "ABCDEF", "short"]
    doc = document_from_projection(projection)
    assert doc.logical_lines == ("0123456789ABCDEF", "short")
    assert doc.text == "0123456789ABCDEF\nshort"
    wrapped = range_for_lines(doc, 0, 0)
    assert wrapped.extract(doc.text) == "0123456789ABCDEF"


def test_wide_wrapped_line_joins_exact() -> None:
    projection = _feed("AB中文CDEF12\r\ntail".encode(), cols=10)
    assert projection.lines == ["AB中文CDEF", "12", "tail"]
    doc = document_from_projection(projection)
    assert doc.text == "AB中文CDEF12\ntail"


def test_scrollback_and_screen_select_as_one_document() -> None:
    projection = _feed(_read("08_scrollback.bin"), rows=24)
    doc = document_from_projection(projection)
    assert doc.line_count == 30
    assert doc.logical_lines[0] == "L01"
    assert doc.logical_lines[-1] == "L30"
    # A range spanning the scrollback/screen boundary copies exactly.
    selection = range_for_lines(doc, 6, 8)
    assert selection.extract(doc.text) == "L07\nL08\nL09"


def test_full_row_hard_newline_joins_documented() -> None:
    # Accepted difference: the projection exposes no wrap flags, so a hard
    # newline right after a full-width row is indistinguishable from a soft
    # wrap and joins. Pinned here so the behavior can only change loudly.
    projection = _feed(b"0123456789\r\nnext", cols=10)
    doc = document_from_projection(projection)
    assert doc.text == "0123456789next"


def test_alt_screen_selects_visible_lines_only() -> None:
    projection = _feed(_read("08_scrollback.bin"), rows=24)
    assert len(projection.scrollback) == 7
    projection.feed(_read("04b_alt_content.bin"))
    projection.flush()
    assert projection.alt_active is True
    doc = document_from_projection(projection)
    assert doc.alt_screen is True
    # Exact visible-lines-only document: no main-screen scrollback leaks in.
    assert doc.logical_lines == ("TUI-content",)
    assert doc.text == "TUI-content"
    for leaked in ("L01", "L07", "L08", "L30"):
        assert leaked not in doc.text

    async def scenario() -> None:
        clipboard = FakeClipboard()
        written = await copy_all(doc.text, clipboard)
        assert written == "TUI-content"
        assert clipboard.writes == ["TUI-content"]
        assert clipboard.writes[0].encode("utf-8") == b"TUI-content"

    run(scenario())


def test_empty_document_is_safe() -> None:
    doc = document_from_rows([], 80)
    assert doc.text == ""
    assert offset_at(doc, 5, 5) == 0
    assert range_for_lines(doc, 0, 3).extract(doc.text) == ""


# -- anchor behavior (normalized selection range) ----------------------------


def test_anchor_focus_order_independent() -> None:
    text = "line1\nline2\nline3"
    assert SelectionRange(0, 5).extract(text) == "line1"
    assert SelectionRange(5, 0).extract(text) == "line1"


def test_missing_anchor_selects_all() -> None:
    text = "abc\ndef"
    assert normalize_range(None, None, text) == SelectionRange(0, len(text))
    assert normalize_range(2, None, text) == SelectionRange(0, len(text))
    assert normalize_range(None, 2, text) == SelectionRange(0, len(text))


def test_out_of_bounds_anchor_selects_all() -> None:
    text = "abc\ndef"
    assert normalize_range(99, 2, text) == SelectionRange(0, len(text))
    assert normalize_range(2, 99, text) == SelectionRange(0, len(text))
    assert normalize_range(-1, 2, text) == SelectionRange(0, len(text))


def test_valid_anchor_stands() -> None:
    text = "abc\ndef"
    assert normalize_range(1, 5, text) == SelectionRange(1, 5)


def test_utf16_offset_mapping_round_trips() -> None:
    text = "A😀éZ"
    assert str_to_utf16_offset(text, 0) == 0
    assert str_to_utf16_offset(text, 1) == 1
    assert str_to_utf16_offset(text, 2) == 3  # astral emoji counts double
    assert str_to_utf16_offset(text, len(text)) == 6
    for units in range(7):
        back = utf16_to_str_offset(text, units)
        assert 0 <= back <= len(text)
    assert utf16_to_str_offset(text, 3) == 2
    assert utf16_to_str_offset(text, 99) == len(text)


# -- explicit copy path -------------------------------------------------------


def test_copy_range_byte_exact() -> None:
    async def scenario() -> None:
        clipboard = FakeClipboard()
        text = "AB中文C\n😀 done\ntail"
        selection = SelectionRange(0, len("AB中文C\n😀 done"))
        written = await copy_range(text, selection, clipboard)
        assert written == "AB中文C\n😀 done"
        assert clipboard.writes == ["AB中文C\n😀 done"]
        assert clipboard.writes[0].encode("utf-8") == "AB中文C\n😀 done".encode()

    run(scenario())


def test_copy_all_and_copy_text() -> None:
    async def scenario() -> None:
        clipboard = FakeClipboard()
        assert await copy_all("x\ny", clipboard) == "x\ny"
        assert await copy_text("", clipboard) == ""
        assert clipboard.writes == ["x\ny", ""]
        assert clipboard.reads == 0  # copy never polls the clipboard

    run(scenario())


def test_clipboard_failure_sets_rendered_error_without_input() -> None:
    """A clipboard failure records a usable explanation in selection state,
    the sheet renders it, and no PTY input is emitted."""

    class RaisingClipboard(ClipboardPort):
        async def get_text(self) -> str | None:
            return None

        async def set_text(self, value: str) -> None:
            raise OSError("clipboard unavailable")

    async def scenario() -> None:
        clipboard = RaisingClipboard()
        inputs: list[tuple[int, bytes]] = []
        state = open_selection(TerminalViewState(lease_mode="control"), anchor=0, focus=5)
        text = "hello world"
        try:
            await copy_range(text, resolved_range(state, text), clipboard)
            raise AssertionError("copy must surface the clipboard failure")
        except OSError as exc:
            state = set_selection_error(state, f"Copy failed: {exc}")
        assert state.selection.error == "Copy failed: clipboard unavailable"
        app = await render_component(
            TerminalScreen,
            state=state,
            on_jump_to_bottom=lambda: None,
            on_size_report=lambda c, r: None,
            on_input=lambda origin, data: inputs.append((origin, data)),
            on_open_selection=lambda: None,
            selection_text=text,
            on_copy_selection=lambda a, f: None,
            on_close_selection=lambda: None,
        )
        assert app.find("select-text-error").value == "Copy failed: clipboard unavailable"
        assert inputs == []

    run(scenario())


def test_live_selection_copies_through_extension_packets() -> None:
    async def scenario() -> None:
        term = RelayDeckTerminal()
        sent: list[bytes] = []
        term._test_mark_ready(sent)
        clipboard = FakeClipboard()
        import asyncio

        async def reply() -> None:
            await asyncio.sleep(0.01)
            from relaydeck_terminal.protocol import encode_selection_text

            term._test_inject_dart_message(encode_selection_text("live 😀 text"))

        task = asyncio.ensure_future(reply())
        try:
            written = await copy_live_selection(term, clipboard)
        finally:
            await task
        assert written == "live 😀 text"
        assert clipboard.writes == ["live 😀 text"]
        kinds = [packet[0] for packet in sent]
        assert kinds == [REQUEST_SELECTION_TEXT]
        clear_live_selection(term)
        assert sent[-1][0] == CLEAR_SELECTION

    run(scenario())


# -- selection/input-focus separation -----------------------------------------


def test_selection_reducers_preserve_everything_else() -> None:
    base = apply_live(TerminalViewState(lease_mode="control"), "e1", 128)
    for updated in (
        open_selection(base, anchor=3, focus=9),
        move_focus(open_selection(base), 5),
        move_anchor(open_selection(base), 2),
        mark_copied(open_selection(base), "copied"),
        set_selection_error(open_selection(base), "denied"),
        close_selection(open_selection(base, anchor=1, focus=2)),
    ):
        assert replace(updated, selection=base.selection) == base
    opened = open_selection(base, anchor=3, focus=9)
    assert opened.selection == SelectionState(open=True, anchor=3, focus=9)
    assert close_selection(opened).selection == SelectionState(open=False)
    assert resolved_range(opened, "0123456789") == SelectionRange(3, 9)
    assert resolved_range(base, "abc") == SelectionRange(0, 3)


def test_selection_enters_and_exits_without_input_frames() -> None:
    async def scenario() -> None:
        inputs: list[tuple[int, bytes]] = []
        state = apply_live(TerminalViewState(lease_mode="control"), "e1", 0)
        state = open_selection(state)
        app = await render_component(
            TerminalScreen,
            state=state,
            on_jump_to_bottom=lambda: None,
            on_size_report=lambda c, r: None,
            on_input=lambda origin, data: inputs.append((origin, data)),
            on_open_selection=lambda: None,
            selection_text="hello",
            on_copy_selection=lambda a, f: None,
            on_close_selection=lambda: None,
        )
        # Sheet copy and close flow through explicit callbacks only. `inputs`
        # is the mounted screen's own sender, so an empty list proves the
        # selection flow emitted no PTY input.
        await app.set_props_and_fire("select-text-copy", {}, "click")
        await app.set_props_and_fire("select-text-close", {}, "click")
        assert inputs == []

    run(scenario())


def test_selection_module_has_no_input_or_draft_surface() -> None:
    # Structural guarantee: the selection domain imports only the clipboard
    # port, terminal models/projection, and the stdlib — no input
    # controller, stream client, socket, or composer/draft module.
    import relaydeck.client.features.terminal.selection_model as model

    tree = ast.parse(open(model.__file__ or "").read())
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module)
        elif isinstance(node, ast.Import):
            imported.update(alias.name for alias in node.names)
    allowed = {
        "TYPE_CHECKING",
        "__future__",
        "collections.abc",
        "dataclasses",
        "relaydeck.client.adapters.ports",
        "relaydeck.client.features.terminal.models",
        "relaydeck.client.features.terminal.projection",
        "relaydeck_terminal.terminal",
        "typing",
        "unicodedata",
    }
    assert imported <= allowed, imported - allowed


def test_selection_flow_leaves_composer_draft_byte_identical() -> None:
    """real draft regression test (replaces the probe).

    A live ComposerStore draft rides alongside the full selection flow
    (open/copy/mark/close). Selection resolves against terminal text only;
    the composer draft must stay byte-identical and no send may occur.
    """

    async def scenario() -> None:
        from relaydeck.client.features.composer.domain import SUBMISSION_IDLE, ComposerStore

        async def _submit(command: dict[str, Any]) -> dict[str, Any]:
            raise AssertionError("selection must never submit a command")

        async def _prompt(target: str, text: str) -> dict[str, Any]:
            raise AssertionError("selection must never prompt")

        store = ComposerStore(
            target="w1:p1",
            submit_command=_submit,
            prompt_text=_prompt,
            host_id="home-mac",
            host_epoch="epoch-7",
            agent_id="w1:p1",
        )
        draft = "unsent multiline\ndraft \U0001f600"
        store.replace_draft(draft)
        clipboard = FakeClipboard()
        state = open_selection(TerminalViewState(), anchor=0, focus=5)
        text = "hello world"
        copied = await copy_range(text, resolved_range(state, text), clipboard)
        marked = mark_copied(state, copied)
        assert clipboard.writes == ["hello"]
        assert marked.selection.last_copied == "hello"
        closed = close_selection(marked)
        assert closed.selection.open is False
        assert store.draft == draft
        assert store.draft.encode("utf-8") == draft.encode("utf-8")
        assert store.messages == ()
        assert store.submission == SUBMISSION_IDLE

    run(scenario())


# -- sheet and screen rendering -------------------------------------------------


def test_sheet_renders_anchored_readonly_mono() -> None:
    async def scenario() -> None:
        copied: list[tuple[int, int]] = []
        closed: list[bool] = []
        app = await render_component(
            SelectTextSheet,
            text="alpha\nbeta",
            anchor=6,
            focus=10,
            on_copy=lambda a, f: copied.append((a, f)),
            on_close=lambda: closed.append(True),
        )
        assert app.find("select-text-title").value == "Select Text"
        assert app.find("select-text-range").value == "Chars 6-10 of 10 (1 lines)"
        field = app.find("select-text-field")
        assert field.value == "alpha\nbeta"
        assert field.read_only is True
        assert field.multiline is True
        assert field.autofocus is False
        assert field.text_style.font_family == "monospace"
        assert field.text_style.size == 14
        assert (field.selection.base_offset, field.selection.extent_offset) == (6, 10)
        await app.set_props_and_fire("select-text-copy", {}, "click")
        assert copied == [(6, 10)]
        await app.set_props_and_fire("select-text-close", {}, "click")
        assert closed == [True]

    run(scenario())


def test_sheet_defaults_to_all_text() -> None:
    async def scenario() -> None:
        app = await render_component(
            SelectTextSheet,
            text="ab",
            anchor=None,
            focus=None,
            on_copy=lambda a, f: None,
            on_close=lambda: None,
        )
        assert app.find("select-text-range").value == "All text (2 chars)"
        field = app.find("select-text-field")
        assert (field.selection.base_offset, field.selection.extent_offset) == (0, 2)

    run(scenario())


def test_sheet_emoji_anchor_uses_utf16_units() -> None:
    async def scenario() -> None:
        text = "A😀Z"
        app = await render_component(
            SelectTextSheet,
            text=text,
            anchor=0,
            focus=2,
            on_copy=lambda a, f: None,
            on_close=lambda: None,
        )
        field = app.find("select-text-field")
        # str offsets (0, 2) cover "A😀": UTF-16 units (0, 3).
        assert (field.selection.base_offset, field.selection.extent_offset) == (0, 3)

    run(scenario())


def test_range_label_matrix() -> None:
    assert range_label("", 0, 0) == "No output to select"
    assert range_label("ab", 0, 2) == "All text (2 chars)"
    assert range_label("a\nb\nc", 0, 5) == "All text (5 chars)"
    assert range_label("a\nb\nc", 0, 3) == "Chars 0-3 of 5 (2 lines)"


def test_screen_mounts_select_text_entry_and_sheet() -> None:
    async def scenario() -> None:
        opened: list[bool] = []
        base = TerminalViewState(lease_mode="view")
        app: HostApp = await render_component(
            TerminalScreen,
            state=base,
            on_jump_to_bottom=lambda: None,
            on_size_report=lambda c, r: None,
            on_input=lambda o, d: None,
            on_open_selection=lambda: opened.append(True),
        )
        assert button_label(app.find("terminal-select-text")) == "Select text"
        try:
            app.find("select-text-sheet")
            raise AssertionError("sheet must stay hidden until selection opens")
        except KeyError:
            pass
        await app.set_props_and_fire("terminal-select-text", {}, "click")
        assert opened == [True]

        shown = open_selection(base)
        app2 = await render_component(
            TerminalScreen,
            state=shown,
            on_jump_to_bottom=lambda: None,
            on_size_report=lambda c, r: None,
            on_input=lambda o, d: None,
            on_open_selection=lambda: None,
            selection_text="out",
            on_copy_selection=lambda a, f: None,
            on_close_selection=lambda: None,
        )
        assert app2.find("select-text-sheet") is not None
        assert app2.find("select-text-title").value == "Select Text"
        assert app2.find("terminal-surface") is not None

    run(scenario())


def test_screen_without_selection_callbacks_omits_selection_ui() -> None:
    async def scenario() -> None:
        app = await render_component(
            TerminalScreen,
            state=TerminalViewState(lease_mode="view"),
            on_jump_to_bottom=lambda: None,
            on_size_report=lambda c, r: None,
            on_input=lambda o, d: None,
        )
        assert app.find("terminal-surface") is not None
        for key in ("terminal-select-text", "select-text-sheet"):
            try:
                app.find(key)
                raise AssertionError(f"{key} must be absent without selection wiring")
            except KeyError:
                pass

    run(scenario())
