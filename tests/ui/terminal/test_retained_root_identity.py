"""Retained Root/engine identity on Flet 1.0.3; renderer/device proof pending."""

from __future__ import annotations

from dataclasses import replace
from typing import Any

import pytest
from relaydeck_terminal.protocol import ORIGIN_KEY

from relaydeck.client.components.appearance import AppearanceBridge
from relaydeck.client.features.inventory.routing import RouteBridge
from relaydeck.client.features.terminal.agent_session import AgentSession
from relaydeck.client.features.terminal.callbacks import foreground_action
from relaydeck.client.features.terminal.input_modes import (
    ROUTE_PTY,
    WORKFLOW_AGENT_COMPOSER,
    WORKFLOW_AGENT_DIRECT,
)
from relaydeck.client.retained_sessions import SLOT_IDLE_MS as RETENTION_IDLE_MS
from relaydeck.contracts.wire import terminal as terminal_wire
from tests.support.host_harness import run
from tests.support.sync.transport_fixtures import _wait
from tests.support.ui.retained_root import _active, _open, _render


def test_console_breakpoints_collapse_and_theme_preserve_actual_view_binding(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    binds: list[AgentSession] = []
    unbinds: list[AgentSession] = []
    original_bind, original_unbind = AgentSession.bind_view, AgentSession.unbind_view

    def bind(session: AgentSession, term: Any, **kwargs: Any) -> int:
        binds.append(session)
        return original_bind(session, term, **kwargs)

    def unbind(session: AgentSession) -> None:
        unbinds.append(session)
        original_unbind(session)

    monkeypatch.setattr(AgentSession, "bind_view", bind)
    monkeypatch.setattr(AgentSession, "unbind_view", unbind)

    async def scenario() -> None:
        bridge = RouteBridge(width=1440, height=1000)
        appearance = AppearanceBridge(True)
        app, controller, sessions, pool, now = await _render(
            bridge=bridge,
            appearance=appearance,
        )
        try:
            await _open(app, "home-mac:term-1")
            session = sessions.factory.created[0]
            await app.set_props_and_fire(
                "composer-draft", {"value": "layout draft"}, "change", "layout draft"
            )
            session.set_workflow(WORKFLOW_AGENT_DIRECT)
            await app.settle()
            token, engine = session.input_token, session.current_term
            before = (len(binds), len(unbinds), len(sessions.ops.attaches))
            # Declarative controls are reconstructed; Flet's reconciled IDs and
            # real bind/cleanup effects identify the mounted parent lifetime.
            parent_keys = (
                "inventory-retained-workbench",
                "retained-layer",
                "retained-surfaces",
            )
            parent_ids = tuple(app.find(key)._i for key in parent_keys)
            for width in (1099, 1100, 599, 600, 360, 390, 768, 1440):
                bridge.set_width(width)
                await app.settle()
                assert tuple(app.find(key)._i for key in parent_keys) == parent_ids
                assert app.find("retained-layer").content._i == parent_ids[-1]
                assert (
                    app.find("retained-layer") in app.find("inventory-retained-workbench").controls
                )
                assert session.current_term is engine and session.input_token == token
                assert (len(binds), len(unbinds), len(sessions.ops.attaches)) == before
            await app.set_props_and_fire("console-collapse-toggle", {}, "click")
            assert app.find("console-strip") is not None
            await app.set_props_and_fire("sidebar-show", {}, "click")
            assert (len(binds), len(unbinds), len(sessions.ops.attaches)) == before
            for dark in (False, True):
                appearance.set_dark(dark)
                await app.settle()
                assert (len(binds), len(unbinds), len(sessions.ops.attaches)) == before
                assert session.input_token == token
            session.set_workflow(WORKFLOW_AGENT_COMPOSER)
            await app.settle()
            assert app.find("composer-draft").value == "layout draft"
            session.set_workflow(WORKFLOW_AGENT_DIRECT)
            await app.settle()
            assert session.lease is not None
            socket = next(
                item[2] for item in sessions.streams.opened if item[0] == session.lease.lease_id
            )
            previous = len(socket.sent)
            assert await session.handle_view_input(ORIGIN_KEY, b"x", token=token) == ROUTE_PTY
            assert len(socket.sent) == previous + 1
            chunk = terminal_wire.decode_input(socket.sent[-1][1])
            assert chunk.data == b"x" and chunk.generation == session.lease.generation
        finally:
            await app.set_props_and_fire("test-unmount", {}, "click")
        assert unbinds and all(s.disposed for s in sessions.factory.created)

    run(scenario())


def test_root_a_b_a_keeps_engine_controller_stream_and_draft_with_stale_actions_fenced() -> None:
    async def scenario() -> None:
        app, controller, sessions, pool, now = await _render()
        try:
            await _open(app, "home-mac:term-1")
            a = sessions.factory.created[0]
            engine, stream_controller = a.current_term, a.controller
            assert engine is not None
            token = a.input_token
            old_composer = a.composer_callbacks()
            old_close = foreground_action(a, a.disconnect)
            old_paste = foreground_action(a, a.confirm_paste)
            await app.set_props_and_fire(
                "composer-draft", {"value": "first draft"}, "change", "first draft"
            )
            # The list picks the session (no session dropdown).
            await app.set_props_and_fire("agent-home-mac:term-2", {}, "click")
            await app.settle()
            await app.set_props_and_fire("agent-takeover-confirm", {}, "click")
            await app.settle()
            b = sessions.factory.created[1]
            assert (
                not a.foreground
                and a.current_term is engine
                and a.controller is not stream_controller
                and a.lease.mode == "view"
            )
            assert pool.get("home-mac", "agent", "home-mac:term-1").session is a
            assert await a.handle_view_input(1, b"late", token=token) == "dropped"
            assert await old_close() == "ignored"
            assert await old_paste() == "ignored"
            assert await old_composer["on_send"]() == "ignored"
            socket = next(
                record[2] for record in sessions.streams.opened if record[0] == a.lease.lease_id
            )
            socket.queue(("bytes", terminal_wire.encode_output(a.lease.epoch, 0, b"offscreen")))
            await _wait(lambda: bytes(a.output_log) == b"offscreen")
            await app.set_props_and_fire(
                "composer-draft", {"value": "second draft"}, "change", "second draft"
            )
            await app.set_props_and_fire("agent-home-mac:term-1", {}, "click")
            await app.settle()
            assert (
                pool.foreground.session is a and a.current_term is engine and a.lease.mode == "view"
            )
            assert not b.foreground and len(sessions.ops.attaches) == 2
            # Parked windows stay mounted (disabled, off screen); read the
            # composer of the active surface only.
            assert _active(app, "composer-draft").value == "first draft"
            assert await old_close() == "ignored"
            assert await old_paste() == "ignored"
            assert await old_composer["on_send"]() == "ignored"
            await app.settle()
            assert pool.foreground is not None
            assert pool.foreground.session is a
            # Wide layout has no Back: leaving the destination hides it.
            await app.set_props_and_fire("console-rail-terminals", {}, "click")
            assert pool.foreground is None and a.current_term is engine
            await app.set_props_and_fire("console-rail-console", {}, "click")
            await app.set_props_and_fire("agent-home-mac:term-1", {}, "click")
            await app.settle()
            await app.settle()
            assert pool.foreground is not None
            assert pool.foreground.session is a and a.current_term is engine
            assert len(sessions.ops.attaches) == 2 and len(sessions.factory.created) == 2
            assert not sessions.ops.closes and sessions.submitted == [] and sessions.prompted == []
        finally:
            await app.set_props_and_fire("test-unmount", {}, "click")
        assert not pool.entries() and a.disposed and b.disposed
        assert not a.output_log and not a.composer_props()["draft"]
        assert not sessions.ops.closes

    run(scenario())


def test_root_sixth_session_lru_and_idle_expiry_never_close_remote_processes() -> None:
    async def scenario() -> None:
        app, controller, sessions, pool, now = await _render()
        try:
            await _open(app, "home-mac:term-1")
            first = sessions.factory.created[0]
            for index in range(2, 7):
                now[0] += 1000
                await app.set_props_and_fire(f"agent-home-mac:term-{index}", {}, "click")
                await app.settle()
                await app.set_props_and_fire("agent-takeover-confirm", {}, "click")
                await app.settle()
            assert first.disposed and len(pool.entries()) == 5
            assert len(sessions.factory.created) == len(sessions.ops.attaches) == 6
            visible = pool.foreground
            now[0] += RETENTION_IDLE_MS
            await pool.evict_idle()
            await app.settle()
            assert pool.entries() == (visible,) and not visible.session.disposed
            assert visible.session.current_term is not None
            # Wide layout has no Back: leaving the destination hides it.
            await app.set_props_and_fire("console-rail-terminals", {}, "click")
            now[0] += RETENTION_IDLE_MS
            await pool.evict_idle()
            await app.settle()
            assert not pool.entries() and visible.session.disposed
            assert not sessions.ops.closes
        finally:
            await app.set_props_and_fire("test-unmount", {}, "click")

    run(scenario())


@pytest.mark.parametrize("boundary", ["host", "epoch", "logout"])
def test_root_duplicate_ids_and_identity_boundaries_clear_retained_data(boundary: str) -> None:
    async def scenario() -> None:
        app, controller, sessions, pool, now = await _render()
        try:
            await _open(app, "home-mac:term-1")
            one = sessions.factory.created[0]
            one.on_draft_change("private one")
            await _open(app, "other-mac:term-1")
            two = sessions.factory.created[1]
            old_engine = two.current_term
            two.on_draft_change("private two")
            assert [record["host_id"] for record in sessions.ops.attaches] == [
                "home-mac",
                "other-mac",
            ]
            assert len(pool.entries()) == 2
            saved = two.composer_callbacks()
            if boundary == "host":
                controller.push(replace(controller.state, granted_hosts=frozenset({"home-mac"})))
            elif boundary == "epoch":
                hosts = dict(controller.state.inventory.hosts)
                hosts["other-mac"] = replace(hosts["other-mac"], epoch="epoch-new")
                controller.push(
                    replace(
                        controller.state, inventory=replace(controller.state.inventory, hosts=hosts)
                    )
                )
            else:
                controller.push(replace(controller.state, revoked=True))
            await app.settle(30)
            assert two.disposed and not two.composer_props()["draft"] and not two.output_log
            assert await saved["on_send"]() == "ignored"
            assert len(pool.entries()) == (0 if boundary == "logout" else 1)
            assert one.disposed == (boundary == "logout")
            assert not sessions.ops.closes and not sessions.submitted
            if boundary == "epoch":
                await app.set_props_and_fire("agent-other-mac:term-1", {}, "click")
                await app.settle()
                await app.settle()
                replacement = sessions.factory.created[-1]
                assert replacement is not two and replacement.current_term is not old_engine
                assert replacement.composer_props()["draft"] == ""
                assert len(sessions.ops.attaches) == 2  # Fresh disclosure is still required.
        finally:
            await app.set_props_and_fire("test-unmount", {}, "click")

    run(scenario())
