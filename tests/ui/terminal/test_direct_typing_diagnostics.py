"""Direct typing has no edit trace; optional reset diagnostics contain no content."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import flet as ft
import pytest

from relaydeck.client.features.terminal.diagnostics import diagnostics_logger
from relaydeck.client.features.terminal.input_modes import ROUTE_PTY
from relaydeck.client.features.terminal.surfaces import direct_typing
from tests.support.host_harness import render_component, run


@pytest.mark.parametrize("diagnostics", [None, "0", "1"])
def test_direct_typing_never_logs_edits_or_reads_the_timing_clock(
    diagnostics: str | None, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    if diagnostics is None:
        monkeypatch.delenv("RELAYDECK_TERMINAL_DIAGNOSTICS", raising=False)
    else:
        monkeypatch.setenv("RELAYDECK_TERMINAL_DIAGNOSTICS", diagnostics)

    def no_edit_clock() -> float:
        raise AssertionError("typing must not collect inter-edit timing")

    monkeypatch.setattr(direct_typing, "_typing_now", no_edit_clock)
    logger = diagnostics_logger()
    logger.addHandler(caplog.handler)
    secret = "PRIVATE-dictation-739"
    sent: list[bytes] = []

    async def scenario() -> None:
        async def send(_origin: int, data: bytes) -> str:
            sent.append(data)
            return ROUTE_PTY

        app = await render_component(
            direct_typing.DirectTypingField,
            on_input=send,
            visible=True,
            enabled=True,
            binding=1,
            field_ref=ft.Ref[ft.TextField](),
            focus_requests=0,
            focus_handled=SimpleNamespace(current=0),
            dark=True,
            field_key="direct-diagnostic-field",
        )
        try:
            for value in ("  " + secret, "  " + secret + "!", "  " + secret + "?"):
                await app.set_props_and_fire(
                    "direct-diagnostic-field",
                    {
                        "value": value,
                        "selection": {"base_offset": len(value), "extent_offset": len(value)},
                    },
                    "change",
                    value,
                )
            assert sent == [secret.encode(), b"!", b"\x7f", b"?"]
        finally:
            app.session.close()

    try:
        run(scenario())
    finally:
        logger.removeHandler(caplog.handler)
    assert not [r for r in caplog.records if r.name == logger.name]


@pytest.mark.parametrize("diagnostics", [None, "0", "true", "1"])
def test_direct_reset_diagnostic_is_opt_in_content_free_and_once_per_revision(
    diagnostics: str | None, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    if diagnostics is None:
        monkeypatch.delenv("RELAYDECK_TERMINAL_DIAGNOSTICS", raising=False)
    else:
        monkeypatch.setenv("RELAYDECK_TERMINAL_DIAGNOSTICS", diagnostics)
    logger = diagnostics_logger()
    logger.addHandler(caplog.handler)
    secret = "PRIVATE-dictation-739"
    sent: list[bytes] = []

    async def scenario() -> None:
        async def send(_origin: int, data: bytes) -> str:
            sent.append(data)
            return ROUTE_PTY

        field_ref = ft.Ref[ft.TextField]()

        @ft.component
        def Fixture() -> Any:
            revision, set_revision = ft.use_state(0)
            return ft.Column(
                controls=[
                    ft.Button(
                        "render", key="render", on_click=lambda _e: set_revision(revision + 1)
                    ),
                    direct_typing.DirectTypingField(
                        on_input=send,
                        visible=True,
                        enabled=True,
                        binding=1,
                        field_ref=field_ref,
                        focus_requests=0,
                        focus_handled=SimpleNamespace(current=0),
                        dark=True,
                        field_key="direct-diagnostic-field",
                    ),
                ]
            )

        app = await render_component(Fixture)
        try:
            value = "  " + secret
            await app.set_props_and_fire(
                "direct-diagnostic-field", {"value": value}, "change", value
            )
            await app.set_props_and_fire("direct-diagnostic-field", {}, "blur")
            for _ in range(4):
                await app.set_props_and_fire("render", {}, "click")
            assert sent == [secret.encode()]
        finally:
            app.session.close()

    try:
        run(scenario())
    finally:
        logger.removeHandler(caplog.handler)
    lines = [r.getMessage() for r in caplog.records if r.name == logger.name]
    assert lines == (["direct_typing_reset sent=True"] if diagnostics == "1" else [])
    for record in caplog.records:
        assert secret not in record.getMessage() and secret not in repr(record.args)
        assert record.exc_info is None and record.stack_info is None
