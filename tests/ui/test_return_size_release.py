"""Real Root lifecycle actions release the writer and preserve retained output/draft."""

import pytest

from relaydeck.client.features.inventory.routing import RouteBridge
from tests.support.host_harness import button_label, run
from tests.support.ui.instant_inventory import navigate, valid_inventory
from tests.support.ui.instant_open import jobs
from tests.support.ui.navigation import _StubTransport
from tests.support.ui.retained_root import _open, _render


@pytest.mark.parametrize("agent", [True, False])
@pytest.mark.parametrize("trigger", ["release", "leave", "background", "close"])
def test_writer_release_triggers(agent, trigger, monkeypatch) -> None:
    monkeypatch.setattr(_StubTransport, "snapshot", valid_inventory)

    async def scenario():
        app, _controller, scripted, pool, _now = await _render(
            bridge=RouteBridge(width=390, height=844)
        )
        closed = False
        try:
            if agent:
                await _open(app, "home-mac:term-1")
            else:
                await navigate(app, "terminals", 390)
                await app.set_props_and_fire("shell-home-mac:tab-1", {}, "click")
                await app.settle()
            entry = pool.foreground
            session, engine, lease = entry.session, entry.session.current_term, entry.session.lease
            assert lease.mode == "control"
            if agent:
                session.store.replace_draft("unsent draft")
            if trigger == "release":
                key = "agent-disconnect" if agent else "shell-disconnect"
                assert button_label(app.find(key)) == "Release"
                await app.set_props_and_fire(key, {}, "click")
            elif trigger == "leave":
                await navigate(app, "settings", 390)
            elif trigger == "background":
                await app.set_props_and_fire("test-background", {}, "click")
            else:
                await app.set_props_and_fire("test-unmount", {}, "click")
                closed = True
            await jobs(pool)
            await app.settle()
            assert lease.lease_id in scripted.ops.releases
            assert session.controller is None or session.controller.key_generation() is None
            assert all(
                kind != "bytes" for _, _, sock in scripted.streams.opened for kind, _ in sock.sent
            )
            if trigger != "close":
                assert session.current_term is engine
            if agent and trigger != "close":
                assert session.store.draft == "unsent draft"
        finally:
            if not closed:
                await app.set_props_and_fire("test-unmount", {}, "click")

    run(scenario())
