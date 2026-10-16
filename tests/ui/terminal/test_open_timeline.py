"""Opening hold marks follow the rendered session rather than the render task's context."""

import asyncio
from types import SimpleNamespace
from typing import Any

import flet as ft
import pytest

from relaydeck.client import open_timing
from relaydeck.client.features.terminal.surfaces import agent_hooks
from tests.support.host_harness import render_component, run


def test_real_hold_effect_records_the_bound_sessions_arm_and_release(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def scenario() -> None:
        now = [100.0]
        stale = open_timing.OpenTiming("agent", lambda: now[0])
        timing = open_timing.OpenTiming("agent", lambda: now[0])
        session = SimpleNamespace(open_timing=timing)
        open_timing.bind(stale)
        forwarded: list[tuple[int, int]] = []
        sent = asyncio.Event()
        out: dict[str, Any] = {}
        timers: list[tuple[Any, tuple[Any, ...], Any]] = []
        loop = asyncio.get_running_loop()
        call_later = loop.call_later

        def schedule(delay: float, callback: Any, *args: Any, context: Any = None) -> Any:
            if getattr(callback, "__module__", None) != agent_hooks.__name__:
                return call_later(delay, callback, *args, context=context)
            handle = SimpleNamespace(cancelled=False)
            handle.cancel = lambda: setattr(handle, "cancelled", True)
            timers.append((callback, args, handle))
            return handle

        monkeypatch.setattr(loop, "call_later", schedule)

        def forward(cols: int, rows: int) -> None:
            forwarded.append((cols, rows))
            sent.set()

        @ft.component
        def Probe() -> ft.Control:
            report, held = agent_hooks.use_open_hold((80, 24), forward, binding=(session, None, 0))
            out.update(report=report, held=held)
            return ft.Text("probe")

        app = await render_component(Probe)
        try:
            assert timing._marks == {"hold_armed": 0}
            now[0] += 0.6
            out["report"](80, 24)
            for callback, args, handle in tuple(timers):
                if not handle.cancelled:
                    callback(*args)
            await asyncio.wait_for(sent.wait(), 2)
            await app.settle()
            assert timing._marks == {"hold_armed": 0, "hold_released": 600}
            assert not out["held"] and forwarded == [(80, 24)]
            assert stale._marks == {} and not timing.done
        finally:
            app.session.close()

    run(scenario())


@pytest.mark.parametrize("outcome", ["ready", "timeout"])
def test_actual_flet_fresh_start_observes_without_connect_and_closes(
    monkeypatch: pytest.MonkeyPatch, outcome: str
) -> None:
    import httpx
    from flet_web.fastapi.flet_app import FletApp
    from relaydeck_terminal import RelayDeckTerminal, protocol

    from relaydeck.client import resources as resource_module
    from relaydeck.client import serve
    from relaydeck.client.features.terminal.diagnostics import diagnostics_logger
    from relaydeck.gateway.app import create_app
    from tests.support.gateway.flet_gateway import _gateway_state
    from tests.support.scripted_sessions import make_scripted_sessions

    async def scenario() -> None:
        now = [100.0]
        captured = []
        built = []
        build = resource_module.build_resources

        def response(request):
            return httpx.Response(
                200, json={"account": "owner", "device_id": "device", "hosts": []}
            )

        client = httpx.AsyncClient(transport=httpx.MockTransport(response))

        def tracked_build(*args, **kwargs):
            resources = build(*args, **kwargs, client=client)
            built.append(resources)
            return resources

        monkeypatch.setattr(resource_module, "build_resources", tracked_build)

        @ft.component
        def EmptyRoot(**props):
            return ft.Text("synthetic")

        monkeypatch.setattr("relaydeck.client.app.Root", EmptyRoot)
        app = await render_component(lambda: ft.Text("startup"))
        store = serve.WebSessionStore(
            "https://private.test", b"synthetic-cookie-key" * 2, now_ms=lambda: 100_000
        )
        sid = store.create_session("owner", "device", "access", "refresh", 2_000_000)
        store.bind_flet(sid, app.session.id, serve._Handshake("", "synthetic", ""))
        state = _gateway_state()
        main = serve.make_relaydeck_main(
            store=store,
            gateway_state=state,
            gateway_app=create_app(state),
            base_url=store.public_origin,
            host_id="home-mac",
        )
        connects = []

        def entry(page):
            main(page)
            original = page.on_connect

            async def connected(event):
                connects.append(True)
                await original(event)

            page.on_connect = connected

        carrier = object.__new__(FletApp)
        carrier._FletApp__session = app.session
        carrier._FletApp__main = entry
        import logging

        class Lines(logging.Handler):
            def emit(self, record):
                if record.getMessage().startswith("open_timing "):
                    captured.append(record.getMessage())

        logger = diagnostics_logger()
        handler = Lines()
        logger.addHandler(handler)
        scripted = make_scripted_sessions()
        try:
            await carrier._FletApp__on_session_created()
            assert built and connects == []
            resources = built[0]
            assert resources._ui_watchdog is not None
            watchdog = resources._ui_watchdog
            # An idempotent startup does not create another timer/task or enable recovery.
            resources.start_output_observer()
            assert resources._ui_watchdog is watchdog
            assert resources._ui_current is None and resources._ui_transport is None
            session = scripted.factory.make_shell(host_id="home-mac")
            await session.open("shell")
            await resources.retained.acquire("home-mac", "shell", "shell", "e1", lambda: session)
            timing = open_timing.OpenTiming("shell", lambda: now[0])
            session.open_timing = timing
            term = RelayDeckTerminal()
            term._test_mark_ready([])
            session.bind_view(term)
            timing.mark("hold_released")
            if outcome == "ready":
                # Both events precede the first watchdog tick; the terminal stays quiet.
                term.write(b"x")
                term._test_inject_dart_message(protocol.encode_ack(1))
                term._test_inject_dart_message(
                    protocol.encode_view_report(protocol.ViewState(False, True, False))
                )
            else:
                now[0] += open_timing.TIMEOUT_S + 1
            async with asyncio.timeout(2):
                while not timing.done:
                    await asyncio.sleep(0.01)
            assert len(captured) == 1 and f"outcome={outcome}" in captured[0]
            assert connects == [] and resources._ui_transport is None
            assert resources._ui_output == {} and resources.ui_resumes._active == {}
        finally:
            for resources in built:
                await resources.close()
                if resources._ui_watchdog is not None:
                    assert resources._ui_watchdog.cancelled()
            logger.removeHandler(handler)
            await client.aclose()
            app.session.close()

    run(scenario())
