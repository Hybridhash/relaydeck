"""Owner's tab round trip uses actual Root controls and retained sessions."""

from __future__ import annotations

from typing import Any

import pytest
from flet.messaging.protocol import MessageAction

from relaydeck.client.features.a11y.focus import iter_controls
from relaydeck.client.features.inventory.routing import RouteBridge
from relaydeck.client.packaged_runtime import ScopedDrafts
from relaydeck.contracts.wire import terminal as terminal_wire
from tests.support.fakes import FakeCredentialStore
from tests.support.host_harness import run
from tests.support.pictures.attachments import (
    IDENTIFIER,
    PATH,
    PREVIEW,
    _Api,
    choose,
    install_picker,
)
from tests.support.ui.instant_inventory import valid_inventory
from tests.support.ui.instant_open import jobs
from tests.support.ui.management import _render as _management_root
from tests.support.ui.navigation import _StubTransport
from tests.support.ui.retained_root import _active, _open, _render


async def _fire(app: Any, key: str, event: str = "click", value: str | None = None) -> None:
    control = _active(app, key)
    if value is not None:
        app.session.apply_patch(control._i, {"value": value})
    await app.session.dispatch_event(control._i, event, value or "")
    await app.settle()


async def _compose(app: Any) -> None:
    # Narrow layouts start composing from the tools popover; wider ones focus the text box.
    try:
        await _fire(app, "terminal-tools-button")
        await _fire(app, "tools-mode-compose")
    except KeyError:
        await _fire(app, "composer-draft", "focus")


async def _direct(app: Any) -> None:
    # Narrow layouts open Direct input from the tools popover.
    try:
        await _fire(app, "agent-workflow-toggle")
    except KeyError:
        await _fire(app, "terminal-tools-button")
        await _fire(app, "tools-mode-direct")


async def _tab(app: Any, tab: str, width: int) -> None:
    # Hosts is a Settings section now: the "hosts" tab opens Settings, where the host list lives.
    destination = "settings" if tab == "hosts" else tab
    prefix = "console-nav" if width < 600 else "console-rail"
    await _fire(app, f"{prefix}-{destination}")


@pytest.mark.parametrize("width,back", [(390, "button"), (390, "route"), (1280, "route")])
@pytest.mark.parametrize("tab", ["console", "terminals"])
def test_tab_round_trip_after_back_stays_on_list(
    width: int, back: str, tab: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(_StubTransport, "snapshot", valid_inventory)

    async def scenario() -> None:
        bridge = RouteBridge(width=width, height=844)
        app, controller, scripted, pool, _now = await _render(bridge=bridge)
        try:
            await _open(app, "home-mac:term-1")
            agent = pool.foreground.session
            if width < 1100:
                await _compose(app)
            await _fire(app, "composer-draft", "change", "unsent tab draft")
            assert agent.store.draft == "unsent tab draft"
            if tab == "terminals":
                if width < 600:
                    await _fire(app, "terminal-back")
                await _tab(app, "terminals", width)
                await _fire(app, "shell-home-mac:tab-1")
            session = pool.foreground.session
            engine = session.current_term
            if back == "button":
                await _fire(app, "terminal-back" if tab == "console" else "shell-back")
            else:
                bridge.set_route("/agents" if tab == "console" else "/terminals")
                await app.settle()
            await jobs(pool)
            assert pool.foreground is None
            before = (
                len(scripted.ops.attaches),
                len(scripted.ops.leases),
                len(scripted.streams.opened),
            )
            await _tab(app, "terminals" if tab == "console" else "console", width)
            await _tab(app, tab, width)
            await jobs(pool)
            assert bridge.state.selection == ""
            assert controller.state.selection.session_key == ""
            assert pool.foreground is None
            _active(app, "agent-home-mac:term-1" if tab == "console" else "shell-home-mac:tab-1")
            assert agent.store.draft == "unsent tab draft"
            assert session.current_term is engine and not session.disposed
            assert before == (
                len(scripted.ops.attaches),
                len(scripted.ops.leases),
                len(scripted.streams.opened),
            )
            assert not scripted.submitted and not scripted.prompted
            assert not any(
                kind == "bytes" for _, _, sock in scripted.streams.opened for kind, _ in sock.sent
            )
        finally:
            await app.set_props_and_fire("test-unmount", {}, "click")

    run(scenario())


@pytest.mark.parametrize("width", [390, 1280])
@pytest.mark.parametrize("tab", ["console", "terminals"])
def test_list_filters_and_scroll_survive_tab_round_trip(width: int, tab: str) -> None:
    async def scenario() -> None:
        app, _controller, scripted, _pool, _now = await _render(
            bridge=RouteBridge(width=width, height=844)
        )
        try:
            send = app.conn.send_message

            def ack(message: Any) -> None:
                send(message)
                if (
                    message.action == MessageAction.INVOKE_METHOD
                    and message.body.name == "scroll_to"
                ):
                    body = message.body
                    app.session.handle_invoke_method_results(
                        body.control_id, body.call_id, None, None
                    )

            app.conn.send_message = ack
            if tab == "terminals":
                await _tab(app, tab, width)
            await _fire(
                app, "host-chip-home-mac" if tab == "console" else "shell-host-chip-home-mac"
            )
            await _fire(
                app,
                "host-chip-space-home-mac:w1"
                if tab == "console"
                else "shell-space-chip-home-mac:w1",
            )
            key = "agents-tiles" if tab == "console" else "shells-tiles"
            before = {
                node.key
                for node in iter_controls([_active(app, key)])
                if getattr(node, "key", "") and str(node.key).startswith(("agent-", "shell-"))
            }
            await app.session.dispatch_event(
                _active(app, key)._i,
                "scroll",
                {
                    "event_type": "update",
                    "pixels": 123.0,
                    "min_scroll_extent": 0.0,
                    "max_scroll_extent": 800.0,
                    "viewport_dimension": 400.0,
                },
            )
            start = len(app.conn.sent)
            await _tab(app, "terminals" if tab == "console" else "console", width)
            await _tab(app, tab, width)
            scroller = _active(app, key)
            assert any(
                message.action == MessageAction.INVOKE_METHOD
                and message.body.name == "scroll_to"
                and message.body.control_id == scroller._i
                and message.body.args["offset"] == 123.0
                for message in app.conn.sent[start:]
            )
            after = {
                node.key
                for node in iter_controls([scroller])
                if getattr(node, "key", "") and str(node.key).startswith(("agent-", "shell-"))
            }
            assert before == after
            selected = [
                node
                for node in iter_controls([app.page.views[0].controls], active_only=True)
                if type(node).__name__ == "Semantics" and getattr(node, "selected", False)
            ]
            assert any("web" in node.label for node in selected)
            assert not scripted.ops.attaches and not scripted.ops.leases
        finally:
            await app.set_props_and_fire("test-unmount", {}, "click")

    run(scenario())


@pytest.mark.parametrize("width", [390, 1280])
@pytest.mark.parametrize("tab", ["hosts", "settings"])
@pytest.mark.parametrize("reselect", [False, True])
def test_hosts_and_settings_return_to_main_without_confirming_operations(
    width: int, tab: str, reselect: bool
) -> None:
    async def scenario() -> None:
        (
            app,
            http,
            state,
            _controller,
            _public,
            fingerprint,
            _host_token,
            other,
            _refresh,
            logged_out,
        ) = await _management_root(width)
        try:
            await _tab(app, tab, width)
            if tab == "hosts":
                await _fire(app, "host-home-mac")
                assert _active(app, "managed-host-fingerprint").value == fingerprint
                await _fire(app, "managed-host-revoke")
            else:
                await _fire(app, "managed-device-revoke-other-login")
            _active(app, "management-revoke-confirm")
            if not reselect:
                await _tab(app, "console", width)
            await _tab(app, tab, width)
            with pytest.raises(KeyError):
                _active(app, "management-revoke-confirm")
            if tab == "hosts":
                _active(app, "managed-host-select-hint")
                with pytest.raises(KeyError):
                    _active(app, "managed-host-fingerprint")
            else:
                _active(app, "settings-page")
            assert state.sessions.lookup(other, state.now_ms()) is not None and not logged_out
            assert state.hosts.lookup(_host_token) is not None
        finally:
            await app.set_props_and_fire("test-unmount", {}, "click")
            await http.aclose()
            state.push.close()

    run(scenario())


@pytest.mark.parametrize("width", [390, 1280])
def test_navigation_closes_host_setup_but_preserves_its_typed_fields(width: int) -> None:
    async def scenario() -> None:
        app, http, state, *_ = await _management_root(width)
        try:
            await _tab(app, "hosts", width)
            await _fire(app, "enroll-host-open")
            values = {
                "enroll-host-label": "unsent host",
                "enroll-host-key": "unsent public key",
                "enroll-fingerprint": "unsent fingerprint",
            }
            for key, value in values.items():
                await _fire(app, key, "change", value)
            await _tab(app, "console", width)
            await _tab(app, "hosts", width)
            with pytest.raises(KeyError):
                _active(app, "enroll-host-label")
            await _fire(app, "enroll-host-open")
            assert all(_active(app, key).value == value for key, value in values.items())
            assert _active(app, "enroll-fingerprint-confirm").value is False
            assert not state.enrollments._items
        finally:
            await app.set_props_and_fire("test-unmount", {}, "click")
            await http.aclose()
            state.push.close()

    run(scenario())


@pytest.mark.parametrize("width", [390, 1280])
def test_existing_scoped_storage_restores_unsent_compose_and_pictures_after_root_reload(
    width: int, monkeypatch: pytest.MonkeyPatch
) -> None:
    install_picker(monkeypatch, _Api())

    async def scenario() -> None:
        storage = ScopedDrafts(FakeCredentialStore(), "synthetic")
        saved: Any = None
        for reload in (False, True):
            app, _controller, scripted, pool, _now = await _render(
                bridge=RouteBridge(width=width, height=844)
            )
            scripted.factory.draft_store = storage
            try:
                await _open(app, "home-mac:term-1")
                agent = pool.foreground.session
                if width < 1100:
                    await _compose(app)
                if not reload:
                    await _fire(app, "composer-draft", "change", "stored unsent\ntext")
                    await choose(
                        app,
                        "terminal-attach-picture" if width < 1100 else "composer-attach-picture",
                    )
                    saved = agent.store.export_draft()
                    await app.set_props_and_fire("test-background", {}, "click")
                    assert agent.last_pause_persist is not None
                else:
                    assert agent.store.export_draft() == saved
                    assert _active(app, "composer-draft").value == "stored unsent\ntext"
                    _active(app, f"picture-chip-{IDENTIFIER}")
                assert not scripted.submitted and not scripted.prompted
                assert not any(
                    kind == "bytes"
                    for _, _, sock in scripted.streams.opened
                    for kind, _ in sock.sent
                )
            finally:
                await app.set_props_and_fire("test-unmount", {}, "click")

    run(scenario())


@pytest.mark.parametrize("width", [390, 1280])
@pytest.mark.parametrize("form", ["agent", "shell"])
def test_navigation_hides_creation_form_and_preserves_all_typed_fields(
    width: int, form: str
) -> None:
    async def scenario() -> None:
        bridge = RouteBridge(width=width, height=844)
        app, _controller, scripted, pool, _now = await _render(bridge=bridge)
        try:
            if form == "shell":
                await _tab(app, "terminals", width)
            await _fire(app, "console-new-agent" if form == "agent" else "new-terminal-open")
            await _fire(app, f"{form}-workspace-mode", "select", "new")
            values = {
                f"{form}-workspace-directory": "/synthetic/unfinished project",
                f"{form}-workspace-label": "Unfinished space",
                "agent-name" if form == "agent" else "shell-label": "unsent name",
            }
            if form == "agent":
                values["agent-argv"] = "--model 'unsent argument'"
            for key, value in values.items():
                await _fire(app, key, "change", value)
            # Route/back navigation can happen while the modal owns pointer focus.
            bridge.set_route("/settings")
            await app.settle()
            _active(app, "settings-page")
            with pytest.raises(KeyError):
                _active(app, "sheet-submit")
            await _tab(app, "console" if form == "agent" else "terminals", width)
            await _fire(app, "console-new-agent" if form == "agent" else "new-terminal-open")
            for key, value in values.items():
                assert _active(app, key).value == value
            assert _active(app, f"{form}-workspace-mode").value == "new"
            assert not scripted.submitted and not scripted.ops.attaches and pool.foreground is None
            await _fire(app, "sheet-cancel")
            await _fire(app, "console-new-agent" if form == "agent" else "new-terminal-open")
            assert _active(app, "agent-name" if form == "agent" else "shell-label").value in (
                "",
                None,
            )
        finally:
            await app.set_props_and_fire("test-unmount", {}, "click")

    run(scenario())


@pytest.mark.parametrize("width", [390, 1280])
def test_tab_and_agent_round_trip_preserves_compose_text_and_attached_picture(
    width: int, monkeypatch: pytest.MonkeyPatch
) -> None:
    install_picker(monkeypatch, _Api())

    async def scenario() -> None:
        bridge = RouteBridge(width=width, height=844)
        app, _controller, scripted, pool, _now = await _render(bridge=bridge)
        try:
            await _open(app, "home-mac:term-1")
            agent = pool.foreground.session
            if width < 1100:
                await _compose(app)
            await _fire(app, "composer-draft", "change", "exact unsent\ncompose draft")
            await choose(
                app, "terminal-attach-picture" if width < 1100 else "composer-attach-picture"
            )
            saved = agent.store.export_draft()
            assert saved["pictures"][0]["path"] == PATH
            engine = agent.current_term
            # Phone detail hides the bar; the actual Back exposes it. Desktop
            # rail remains available, so switch straight from the open session.
            if width < 600:
                await _fire(app, "terminal-back")
            await _tab(app, "terminals", width)
            await jobs(pool)
            await _tab(app, "console", width)
            assert pool.foreground is None and agent.store.export_draft() == saved
            before = len(scripted.ops.attaches)
            await _fire(app, "agent-home-mac:term-2")
            if width < 600:
                await _fire(app, "terminal-back")
            else:
                bridge.set_route("/agents")
                await app.settle()
            await jobs(pool)
            await _fire(app, "agent-home-mac:term-1")
            assert pool.foreground.session is agent and agent.current_term is engine
            assert agent.store.export_draft() == saved
            assert agent.store.pictures[0].preview == PREVIEW
            _active(app, f"picture-chip-{IDENTIFIER}")
            assert len(scripted.ops.attaches) == before
            assert not scripted.submitted and not scripted.prompted
            assert not any(
                kind == "bytes" for _, _, sock in scripted.streams.opened for kind, _ in sock.sent
            )
        finally:
            await app.set_props_and_fire("test-unmount", {}, "click")

    run(scenario())


@pytest.mark.parametrize("width", [390, 768])
def test_unsent_direct_editor_text_is_retained_without_replaying_it(width: int) -> None:
    async def scenario() -> None:
        bridge = RouteBridge(width=width, height=844)
        app, _controller, scripted, pool, _now = await _render(bridge=bridge)
        try:
            await _open(app, "home-mac:term-1")
            agent = pool.foreground.session
            await _direct(app)
            field = _active(app, "agent-workflow-direct-typing")
            field_id = field._i
            await _fire(app, "agent-workflow-direct-typing", "change", "  abc")
            await app.session.dispatch_event(
                field_id,
                "selection_change",
                {
                    "selected_text": "",
                    "selection": {"base_offset": 2, "extent_offset": 2},
                },
            )
            await app.settle()
            # Native editor state arrived, but no committed change event yet.
            app.session.apply_patch(field_id, {"value": "  unsent😀"})
            bridge.set_route("/agents")
            await app.settle()
            await jobs(pool)
            await _tab(app, "terminals", width)
            await _tab(app, "console", width)
            await _fire(app, "agent-home-mac:term-2")
            bridge.set_route("/agents")
            await app.settle()
            await jobs(pool)
            await _fire(app, "agent-home-mac:term-1")
            assert pool.foreground.session is agent
            entry = pool.get("home-mac", "agent", "home-mac:term-1")
            slot = app.find(f"retained-slot-{entry.render_key}")
            fields = [
                node
                for node in iter_controls([slot])
                if getattr(node, "key", None) == "agent-workflow-direct-typing"
            ]
            assert len(fields) == 1
            assert fields[0]._i == field_id
            assert fields[0].value == "  unsent😀"
            assert fields[0].selection.base_offset == fields[0].selection.extent_offset == 10
            assert [
                terminal_wire.decode_input(data).data
                for _, _, sock in scripted.streams.opened
                for kind, data in sock.sent
                if kind == "bytes"
            ] == [b"abc"]
        finally:
            await app.set_props_and_fire("test-unmount", {}, "click")

    run(scenario())
