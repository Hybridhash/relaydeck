"""Session-local compatibility preserves live results and drops retired IDs."""

from __future__ import annotations

import asyncio
from concurrent.futures import ThreadPoolExecutor
from typing import Any

import flet as ft
import pytest
from flet.messaging.protocol import MessageAction
from flet.messaging.session import Session
from flet.pubsub.pubsub_hub import PubSubHub

from relaydeck.client.flet_invokes import focus_control, install_invoke_result_guard
from tests.support.host_harness import FakeConnection, render_component, run


@pytest.mark.parametrize("error", [None, "synthetic native error"])
def test_invoke_guard_drops_orphan_delivers_registered_and_installs_once(error: str | None) -> None:
    async def scenario() -> None:
        with ThreadPoolExecutor(max_workers=1) as executor:
            conn = FakeConnection()
            conn.loop = asyncio.get_running_loop()
            conn.executor = executor
            conn.pubsubhub = PubSubHub(loop=conn.loop, executor=executor)
            session = Session(conn)
            other = Session(conn)
            try:
                install_invoke_result_guard(session)
                installed = session.handle_invoke_method_results
                install_invoke_result_guard(session)
                assert session.handle_invoke_method_results is installed

                target = session.page._i
                pending = asyncio.create_task(session.invoke_method(target, "focus", {}, timeout=1))
                await asyncio.sleep(0)
                call = conn.sent[-1].body
                orphan = max(*session.index, *other.index) + 1
                session.handle_invoke_method_results(orphan, call.call_id, "ignored", "ignored")
                assert not pending.done()
                session.handle_invoke_method_results(target, call.call_id, "delivered", error)
                if error is None:
                    assert await pending == "delivered"
                else:
                    with pytest.raises(RuntimeError, match="synthetic native error"):
                        await pending
                # Installing on one session must not patch another or hide its errors.
                with pytest.raises(RuntimeError, match="not registered"):
                    other.handle_invoke_method_results(orphan, "missing", None, None)
            finally:
                session.close()
                other.close()

    run(scenario())


def test_invoke_guard_drops_reply_after_native_timeout_and_retirement() -> None:
    async def scenario() -> None:
        with ThreadPoolExecutor(max_workers=1) as executor:
            conn = FakeConnection()
            conn.loop = asyncio.get_running_loop()
            conn.executor = executor
            conn.pubsubhub = PubSubHub(loop=conn.loop, executor=executor)
            session = Session(conn)
            install_invoke_result_guard(session)
            try:
                target = session.page._i
                with pytest.raises(TimeoutError, match="Timeout waiting for invokeMethod"):
                    await session.invoke_method(target, "focus", {}, timeout=0)
                call = conn.sent[-1].body
                del session.index[target]
                session.handle_invoke_method_results(target, call.call_id, None, None)
            finally:
                session.close()

    run(scenario())


@pytest.mark.parametrize("error", [None, "synthetic native error"])
def test_bounded_focus_delivers_registered_success_or_absorbs_native_error(
    error: str | None,
) -> None:
    async def scenario() -> None:
        @ft.component
        def Fixture() -> Any:
            return ft.TextField(key="focus-target")

        app = await render_component(Fixture)
        install_invoke_result_guard(app.session)
        try:
            pending = asyncio.create_task(focus_control(app.find("focus-target")))
            await asyncio.sleep(0)
            call = app.conn.sent[-1]
            assert call.action == MessageAction.INVOKE_METHOD
            assert call.body.name == "focus"
            app.session.handle_invoke_method_results(
                call.body.control_id, call.body.call_id, "delivered", error
            )
            assert await pending is None
            assert not app.session._Session__method_calls
            assert not app.session._Session__method_call_results
        finally:
            app.session.close()

    run(scenario())


def test_bounded_focus_ignores_an_unmounted_control_without_sending() -> None:
    async def scenario() -> None:
        await focus_control(ft.TextField())

    run(scenario())
