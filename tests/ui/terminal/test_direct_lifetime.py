"""Retired Direct fields do not retain their parent callback owners after unmount."""

from __future__ import annotations

import gc
import weakref
from types import SimpleNamespace
from typing import Any

import flet as ft

from relaydeck.client.features.terminal.input_modes import ROUTE_PTY
from relaydeck.client.features.terminal.surfaces.direct_typing import DirectTypingField
from tests.support.host_harness import render_component, run


def test_direct_retired_native_epochs_release_parent_callbacks_after_unmount() -> None:
    async def scenario() -> None:
        sent: list[bytes] = []
        actions: dict[str, Any] = {}
        field = ft.Ref[ft.TextField]()

        class Receiver:
            async def send(self, _origin: int, data: bytes) -> str:
                sent.append(data)
                return ROUTE_PTY

            def allowed(self) -> bool:
                return True

            def handled(self) -> None:
                pass

        owner: dict[str, Any] = {"current": Receiver()}
        receiver = weakref.ref(owner["current"])

        @ft.component
        def Fixture() -> Any:
            mounted, actions["mount"] = ft.use_state(True)
            target = owner.get("current")
            return ft.Column(
                controls=[
                    DirectTypingField(
                        key="direct-component",
                        on_input=target.send,
                        can_input=target.allowed,
                        on_input_handled=target.handled,
                        on_focus=target.handled,
                        visible=True,
                        enabled=True,
                        binding=1,
                        field_ref=field,
                        focus_requests=0,
                        focus_handled=SimpleNamespace(current=0),
                        dark=True,
                        field_key="direct-lifetime",
                    )
                ]
                if mounted
                else []
            )

        app = await render_component(Fixture)
        component = weakref.ref(app.find("direct-component"))
        native = [app.find("direct-lifetime")]
        await app.set_props_and_fire("direct-lifetime", {"value": "  abc"}, "change", "  abc")
        await app.set_props_and_fire("direct-lifetime", {"value": "  abc\n"}, "change", "  abc\n")
        native.append(app.find("direct-lifetime"))
        assert native[0]._i != native[1]._i
        assert receiver() is not None and sent == [b"abc", b"\r"]

        actions["mount"](False)
        await app.settle()
        owner.clear()
        app.session.close()
        await app.settle()
        gc.collect()
        assert receiver() is None
        assert component() is None
        assert app.conn.sent  # Keep the carrier patches and both native epochs reachable.
        for retired in native:
            await app.session.dispatch_event(retired._i, "change", "  stale")
            await app.session.dispatch_event(retired._i, "submit", "")
        assert sent == [b"abc", b"\r"]

    run(scenario())
