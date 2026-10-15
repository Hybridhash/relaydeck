"""OSC52 clipboard-write escapes are never honored.

Malicious output carrying OSC52 (BEL- and ST-terminated, whole and
split across chunks) flows through the real client intake
(byte-stream -> projection) and the payload never becomes screen
text. What is proven here: the intake takes no clipboard sink, so
there is no output->clipboard path to drive (structural absence,
pinned below); the display emulator (xterm2, Dart) leaves
onClipboardStore unset (source-pinned below; no Flutter toolchain
exists here to drive it at runtime); and the only output->clipboard
path is an explicit user copy of a selected range (pinned below).
"""

from __future__ import annotations

import asyncio
from pathlib import Path

from relaydeck.client.adapters.fake import FakeClipboard
from relaydeck.client.features.terminal.projection import TerminalProjection
from relaydeck.client.features.terminal.selection_model import (
    SelectionRange,
    copy_range,
    document_from_projection,
)
from relaydeck.client.features.terminal.stream import TerminalByteStream
from relaydeck.gateway import terminal_wire

OSC52_BEL = b"\x1b]52;c;aGVsbG8gd29ybGQ=\x07"
OSC52_ST = b"\x1b]52;c;aGVsbG8gd29ybGQ=\x1b\\"
OSC52_QUERY = b"\x1b]52;c;?\x07"
OSC52_SPLIT = [b"\x1b]52;c;aGVs", b"bG8gd29ybGQ=", b"\x07"]


def _intake(chunks: list[bytes]) -> TerminalProjection:
    """Real client intake: ordered stream assembly into the oracle."""
    stream = TerminalByteStream()
    projection = TerminalProjection()
    offset = 0
    for piece in chunks:
        event = stream.push(terminal_wire.OutputChunk("e1", offset, piece))
        assert event.kind == "data"
        projection.feed(event.data)
        offset += len(piece)
    projection.flush()
    return projection


def test_osc52_never_writes_clipboard() -> None:
    # The intake below takes no clipboard sink (see
    # test_intake_has_no_clipboard_reference): there is no
    # output->clipboard path for these bytes to take. What this test
    # proves over the real intake is that the payload never even
    # becomes screen text (so no later explicit copy can pick it up).
    projection = _intake([b"before\r\n", OSC52_BEL, b"\r\nafter\r\n", OSC52_ST, OSC52_QUERY])
    text = "\n".join(projection.lines)
    assert "aGVsbG8gd29ybGQ" not in text  # payload never becomes screen text
    assert "before" in text and "after" in text


def test_osc52_split_delivery_never_writes_clipboard() -> None:
    # Split delivery across chunk boundaries (same non-path as above):
    # the payload never becomes screen text.
    projection = _intake([b"start\r\n", *OSC52_SPLIT, b"\r\nend\r\n"])
    text = "\n".join(projection.lines)
    assert "aGVsbG8" not in text and "bG8gd29ybGQ" not in text


def test_explicit_copy_still_writes_exactly_selection() -> None:
    """The only output->clipboard path is an explicit user copy, and it
    writes exactly the selected range."""
    clipboard = FakeClipboard()
    projection = _intake([b"alpha\r\nbeta\r\n"])
    doc = document_from_projection(projection)

    async def scenario() -> None:
        assert clipboard.writes == []
        written = await copy_range(doc.text, SelectionRange(0, 5), clipboard)
        assert written == "alpha"
        assert clipboard.writes == ["alpha"]

    asyncio.run(scenario())


def test_display_emulator_disables_clipboard_store() -> None:
    """xterm2 honors OSC52 only through Terminal.onClipboardStore; the
    bridge must leave it unset. Static pin (no Flutter toolchain here):
    the Dart source states the denial and never assigns the handler."""
    dart = (
        Path(__file__).resolve().parent.parent.parent
        / "extensions"
        / "terminal"
        / "src"
        / "flutter"
        / "relaydeck_terminal"
        / "lib"
        / "src"
        / "relaydeck_terminal.dart"
    )
    source = dart.read_text()
    assert "onClipboardStore" in source  # the denial names the exact hook
    assert "OSC 52" in source
    assert source.count("onClipboardStore =") == 0
    assert source.count("onClipboardStore:") == 0


def test_intake_has_no_clipboard_reference() -> None:
    """Structural: the output-intake modules cannot reach the clipboard
    port (no import, no symbol); clipboard writes exist only behind the
    explicit selection-copy functions."""
    root = Path(__file__).resolve().parent.parent.parent / "src" / "relaydeck"
    for rel in (
        "client/features/terminal/stream.py",
        "client/features/terminal/projection.py",
    ):
        source = (root / rel).read_text()
        assert "ClipboardPort" not in source
        assert "set_text" not in source
        assert "adapters" not in source
