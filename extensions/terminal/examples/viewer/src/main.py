"""Synthetic VT fixture viewer.

Plays the repo VT fixtures through the real extension control with local
echo (INPUT packets loop back as WRITE) so renderer behavior can be
eyeballed on web/Android once the Flutter SDK is provisioned. Not part of
the shipped app. Run from this directory: `flet run --web` (Flutter SDK
required for extensions).
"""

from __future__ import annotations

import pathlib
from typing import Any

import flet as ft
from relaydeck_terminal.protocol import ORIGIN_KEY, ORIGIN_MOUSE, ORIGIN_TEXT
from relaydeck_terminal.terminal import RelayDeckTerminal


def _event_text(event: Any) -> str:
    """Payload-first select/change value (pinned flet 1.0.3): Dart sends
    the new value as the event payload plus a property patch, and the
    patch may land on a different indexed control than dispatch
    resolves — `event.control.value` can be stale."""
    data = getattr(event, "data", None)
    if isinstance(data, str):
        return data
    control = getattr(event, "control", None)
    value = getattr(control, "value", "") if control is not None else ""
    return value if isinstance(value, str) else ""


def _find_fixtures() -> pathlib.Path:
    """Locate tests/fixtures/vt without assuming a repo checkout layout.

    Climbs from this file toward the filesystem root; in a bundle
    (pyodide) where no ancestor carries the fixtures, returns a path
    that does not exist so the viewer renders an empty list and the
    existing missing-fixture status instead of raising IndexError.
    """
    here = pathlib.Path(__file__).resolve()
    candidates = [here.parent, *here.parents]
    for base in candidates:
        vt = base / "tests" / "fixtures" / "vt"
        if vt.is_dir():
            return vt
    return here.parent / "fixtures-vt-missing"


FIXTURES = _find_fixtures()
ORIGINS = {ORIGIN_TEXT: "text", ORIGIN_KEY: "key", ORIGIN_MOUSE: "mouse"}


@ft.component
def Viewer() -> Any:
    fixture, set_fixture = ft.use_state("01_ascii_colors.bin")
    input_on, set_input_on = ft.use_state(True)
    status, set_status = ft.use_state("ready")
    term_box: Any = ft.use_ref(None)

    def on_input(origin: int, data: bytes) -> None:
        # Local echo for the viewer only; the app routes INPUT to the PTY.
        assert term_box.current is not None
        term_box.current.write(data)
        set_status(f"echo {len(data)} bytes origin={ORIGINS.get(origin, origin)}")

    if term_box.current is None:
        term = RelayDeckTerminal(key="viewer-term", input_enabled=True)
        term.add_input_listener(on_input)
        term_box.current = term
    term = term_box.current

    def play() -> None:
        try:
            data = (FIXTURES / fixture).read_bytes()
        except OSError as exc:
            set_status(f"missing fixture: {exc}")
            return
        term.reset()
        term.write(data)
        set_status(f"played {fixture} ({len(data)} bytes)")

    def toggle_input() -> None:
        term.set_input_enabled(not input_on)
        set_input_on(not input_on)

    async def read_selection() -> None:
        try:
            text = await term.get_selection_text(timeout=5)
        except Exception as exc:  # noqa: BLE001 - viewer diagnostics
            set_status(f"selection failed: {exc}")
        else:
            set_status(f"selection: {text!r}")

    async def on_read_selection_click(event: Any) -> None:
        # Real coroutine function: Flet only awaits `async def`
        # handlers, so a sync lambda here would drop read_selection()'s
        # coroutine unawaited and never update the status.
        del event
        await read_selection()

    return ft.SafeArea(
        content=ft.Column(
            controls=[
                ft.Row(
                    controls=[
                        ft.Dropdown(
                            key="fixture",
                            label="Fixture",
                            value=fixture,
                            options=[
                                ft.DropdownOption(text=name, key=name)
                                for name in sorted(
                                    p.name for p in FIXTURES.glob("*.bin") if FIXTURES.exists()
                                )
                            ],
                            on_select=lambda e: set_fixture(_event_text(e)),
                        ),
                        ft.Button("Play", key="play", on_click=lambda e: play()),
                        ft.Button(
                            f"Input: {'on' if input_on else 'off'}",
                            key="toggle",
                            on_click=lambda e: toggle_input(),
                        ),
                        ft.Button(
                            "Read selection",
                            key="selection",
                            on_click=on_read_selection_click,
                        ),
                    ]
                ),
                ft.Text(status, key="status"),
                term,
            ]
        )
    )


def main(page: ft.Page) -> None:
    page.title = "Terminal fixture viewer"
    page.render(Viewer)


if __name__ == "__main__":
    ft.run(main)
