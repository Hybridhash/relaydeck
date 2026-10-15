"""Sheet selection-forwarding regressions.

The TextField reports base/extent in UTF-16 code units while session
state is str offsets: the sheet must convert (astral characters count
double on the wire) and the `selection_change` wrapper must ignore
payloads without a selection instead of crashing.
"""

from __future__ import annotations

import asyncio

from relaydeck.client.features.terminal.events import selection_change
from relaydeck.client.features.terminal.selection_sheet import SelectTextSheet
from tests.support.host_harness import render_component, run


def test_selection_change_wrapper_passes_offsets() -> None:
    async def scenario() -> None:
        seen: list[tuple[int, int]] = []

        async def record(base: int, extent: int) -> None:
            seen.append((base, extent))

        class _Sel:
            base_offset = 2
            extent_offset = 9

        class _Event:
            selection = _Sel()

        await selection_change(record)(_Event())
        assert seen == [(2, 9)]

    asyncio.run(scenario())


def test_selection_change_wrapper_ignores_selection_less_events() -> None:
    async def scenario() -> None:
        called: list[bool] = []

        async def record(base: int, extent: int) -> None:
            called.append(True)

        class _Plain:
            pass

        assert await selection_change(record)(_Plain()) is None
        assert called == []

    asyncio.run(scenario())


def test_sheet_forwards_utf16_as_str_offsets() -> None:
    async def scenario() -> None:
        seen: list[tuple[int, int]] = []
        text = "a\U0001f600b"  # str len 3, UTF-16 len 4
        app = await render_component(
            SelectTextSheet,
            text=text,
            anchor=0,
            focus=3,
            on_copy=lambda a, f: None,
            on_close=lambda: None,
            on_selection_change=lambda a, f: seen.append((a, f)),
        )
        await app.set_props_and_fire(
            "select-text-field",
            {},
            "selection_change",
            {
                "selected_text": "a\U0001f600",
                "selection": {"base_offset": 0, "extent_offset": 3},
            },
        )
        assert seen == [(0, 2)]

    run(scenario())


def test_sheet_without_selection_callback_ignores_drags() -> None:
    async def scenario() -> None:
        copied: list[tuple[int, int]] = []
        app = await render_component(
            SelectTextSheet,
            text="alpha",
            anchor=0,
            focus=5,
            on_copy=lambda a, f: copied.append((a, f)),
            on_close=lambda: None,
        )
        await app.set_props_and_fire(
            "select-text-field",
            {},
            "selection_change",
            {"selected_text": "alp", "selection": {"base_offset": 0, "extent_offset": 3}},
        )
        await app.set_props_and_fire("select-text-copy", {}, "click")
        assert copied == [(0, 5)]

    run(scenario())
