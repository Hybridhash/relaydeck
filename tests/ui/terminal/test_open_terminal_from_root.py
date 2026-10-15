"""Open terminal through the shipped root.

"Open terminal" on an agent/shell detail must construct a REAL session
from the shipped factory, mount the REAL terminal surface, supervise
its stream, and tear it down on Back/logout/revocation — all through
real Flet dispatched events. Sync runs over the scripted two-host
transport; terminal ownership/streaming runs over scripted ops and
sockets (no network, no credentials).
"""

from __future__ import annotations

from relaydeck_terminal.protocol import ORIGIN_KEY

from relaydeck.client.app import Root
from relaydeck.client.features.a11y.focus import iter_controls
from relaydeck.client.features.inventory.routing import RouteBridge
from relaydeck.client.features.shell.screen import SHELL_BACK_KEY, SHELL_SCREEN_KEY
from relaydeck.client.features.sync.backoff import Backoff
from relaydeck.client.features.sync.controller import SyncController
from relaydeck.client.features.terminal.surfaces import (
    AGENT_DETAIL_KEY,
    AGENT_TAKEOVER_CONFIRM_KEY,
    AGENT_TAKEOVER_DISCLOSURE_KEY,
)
from relaydeck.domain import reducers
from relaydeck.domain.models import ConnectionState, scope_key
from relaydeck.gateway import terminal_wire
from tests.support.host_harness import HostApp, render_component, run
from tests.support.scripted_sessions import ScriptedSessions, make_scripted_sessions
from tests.support.ui.open_terminal import AGENT_ID, AGENT_KEY, _host_entry, _TwoHostTransport

SHELL_TAB = "w1:t2"
SHELL_KEY = scope_key("h1", SHELL_TAB)


def _missing(app: HostApp, key: str) -> bool:
    return not any(
        getattr(control, "key", None) == key
        for control in iter_controls([app.page.views[0].controls], active_only=True)
    )


async def _render() -> tuple[HostApp, SyncController, ScriptedSessions, _TwoHostTransport]:
    sessions = make_scripted_sessions()
    transport = _TwoHostTransport()
    controller = SyncController(transport=transport, backoff=Backoff.seeded(1))
    # One screen at a time (600-1099 px): the window carries a Back
    # arrow, which these leave-vs-revoke scenarios exercise. Wide
    # layouts have no Back (the list picks the session).
    bridge = RouteBridge(route="/", width=900)
    app = await render_component(
        Root,
        controller=controller,
        transport=transport,
        host_id="h1",
        bridge=bridge,
        go=lambda route: None,
        session_factory=sessions.factory,
        account="tester",
    )
    await app.settle()
    return app, controller, sessions, transport


async def _open_shell(app: HostApp) -> None:
    await app.set_props_and_fire("console-rail-terminals", {}, "click")
    assert app.find(f"shell-{SHELL_KEY}") is not None
    await app.set_props_and_fire(f"shell-{SHELL_KEY}", {}, "click")
    await app.settle()
    await app.settle()


async def _open_agent(app: HostApp) -> None:
    assert app.find(f"agent-{AGENT_KEY}") is not None
    await app.set_props_and_fire(f"agent-{AGENT_KEY}", {}, "click")
    await app.settle()
    await app.settle()


def test_open_shell_mounts_live_session_and_back_retains() -> None:
    async def scenario() -> None:
        app, _controller, sessions, _transport = await _render()
        await _open_shell(app)
        assert app.find(SHELL_SCREEN_KEY) is not None
        assert sessions.factory.created and len(sessions.factory.created) == 1
        session = sessions.factory.created[0]
        assert session.phase == "live"
        assert session.terminal_id == SHELL_TAB
        # Real ownership path: attach without takeover, control lease,
        # ticket, stream opened against that lease.
        assert sessions.ops.attaches == [
            {
                "host_id": "h1",
                "terminal_id": SHELL_TAB,
                "kind": "shell",
                "takeover": False,
            }
        ]
        assert len(sessions.streams.opened) == 1
        lease_id, ticket, sock = sessions.streams.opened[0]
        assert lease_id == session.lease.lease_id
        assert ticket.startswith("scripted-ticket-")
        # Root supervises run(): stream bytes reach the session log.
        sock.queue(("bytes", terminal_wire.encode_output("e1", 0, b"hi")))
        await app.settle()
        assert bytes(session.output_log) == b"hi"
        # Back retains the native surface but releases control before
        # warming a view lease; the remote attachment survives.
        await app.set_props_and_fire(SHELL_BACK_KEY, {}, "click")
        await app.settle()
        assert _missing(app, SHELL_SCREEN_KEY)
        assert session.disposed is False and not session.foreground
        # Ordinary Back preserves object contents (leave != revoke).
        assert bytes(session.output_log) == b"hi"
        assert sessions.ops.releases == [lease_id]
        assert session.lease.mode == "view" and session.controller.key_generation() is None
        assert sock.closed and len(sessions.ops.attaches) == 1
        assert sessions.ops.closes == []

    run(scenario())


def test_open_agent_discloses_then_confirms_live() -> None:
    async def scenario() -> None:
        app, _controller, sessions, _transport = await _render()
        await _open_agent(app)
        assert app.find(AGENT_DETAIL_KEY) is not None
        # Agent open attaches NOTHING yet: the takeover disclosure must
        # render first and the user confirms explicitly.
        assert sessions.ops.attaches == []
        assert app.find(AGENT_TAKEOVER_DISCLOSURE_KEY) is not None
        await app.set_props_and_fire(AGENT_TAKEOVER_CONFIRM_KEY, {}, "click")
        await app.settle()
        session = sessions.factory.created[0]
        assert session.phase == "live"
        assert sessions.ops.attaches == [
            {
                "host_id": "h1",
                "terminal_id": AGENT_ID,
                "kind": "agent",
                "takeover": True,
            }
        ]
        assert len(sessions.streams.opened) == 1
        await app.set_props_and_fire("terminal-back", {}, "click")
        await app.settle()
        assert _missing(app, AGENT_DETAIL_KEY)
        assert session.disposed is False and not session.foreground

    run(scenario())


def test_bare_deep_link_then_row_open_mounts_agent_detail() -> None:
    """p2 incident: a deep link with a bare upstream id misses the scoped
    maps (empty detail, browser parity), but selecting the row and clicking
    Open terminal afterwards must still mount the agent surface. The click
    must emit a server patch — never a silent dead click."""

    async def scenario() -> None:
        sessions = make_scripted_sessions()
        transport = _TwoHostTransport()
        controller = SyncController(transport=transport, backoff=Backoff.seeded(1))
        bridge = RouteBridge(route="/agents/a1", width=1280)
        app = await render_component(
            Root,
            controller=controller,
            transport=transport,
            host_id="h1",
            bridge=bridge,
            go=lambda route: None,
            session_factory=sessions.factory,
            account="tester",
        )
        await app.settle()
        assert _missing(app, "detail-open")
        # Choosing the row opens the agent window straight
        # away, watching (disclosure armed, nothing attached).
        sent_before = len(app.conn.sent)
        await app.set_props_and_fire(f"agent-{AGENT_KEY}", {}, "click")
        await app.settle()
        assert len(app.conn.sent) > sent_before
        assert app.find(AGENT_DETAIL_KEY) is not None
        assert len(sessions.factory.created) == 1
        assert sessions.ops.attaches == []

    run(scenario())


def test_revocation_closes_shell_before_saved_input_writes() -> None:
    async def scenario() -> None:
        app, controller, sessions, _transport = await _render()
        await _open_shell(app)
        session = sessions.factory.created[0]
        assert session.phase == "live"
        live_token = session.input_token
        assert live_token is not None
        _lease_id, _ticket, sock = sessions.streams.opened[0]
        sock.queue(("bytes", terminal_wire.encode_output("e1", 0, b"hi")))
        await app.settle()
        assert bytes(session.output_log) == b"hi"
        # The verified listing drops h1: Root must dispose the session
        # before it can write again.
        controller.apply_grants([_host_entry("h2")])
        await app.settle()
        assert session.disposed is True
        assert sock.closed is True
        assert _missing(app, SHELL_SCREEN_KEY)
        assert app.find("destinations") is not None
        # Cached output is wiped on authorization loss.
        assert bytes(session.output_log) == b""
        # Saved writer paths fail closed: traffic identical before
        # and after, no input (bytes) frame, teardown bye retained.
        traffic_before = list(sock.sent)
        assert ("text", '{"kind": "bye"}') in traffic_before
        assert await session.handle_view_input(ORIGIN_KEY, b"x", token=live_token) == ("dropped")
        assert await session.type_text("x") == "dropped"
        assert sock.sent == traffic_before
        assert all(kind != "bytes" for kind, _ in sock.sent)
        # And the next controller emission cannot resurrect h1.
        controller._emit()
        await app.settle()
        assert set(controller.state.inventory.hosts) == {"h2"}
        assert _missing(app, SHELL_SCREEN_KEY)

    run(scenario())


def test_revocation_fences_saved_agent_send_and_clears_draft() -> None:
    async def scenario() -> None:
        app, controller, sessions, _transport = await _render()
        await _open_agent(app)
        await app.set_props_and_fire(AGENT_TAKEOVER_CONFIRM_KEY, {}, "click")
        await app.settle()
        session = sessions.factory.created[0]
        assert session.phase == "live"
        session.on_draft_change("hello agent")
        saved_send = session.composer_callbacks()["on_send"]
        controller.apply_grants([_host_entry("h2")])
        await app.settle()
        assert session.disposed is True
        assert _missing(app, AGENT_DETAIL_KEY)
        assert await saved_send() == "ignored"
        assert sessions.submitted == []
        assert sessions.prompted == []
        # Verified authorization loss clears the unsent draft
        # (reason-aware dispose); ordinary Back preserves it instead.
        assert session.composer_props()["draft"] == ""

    run(scenario())


def test_global_revocation_closes_fences_clears_and_refuses_saved() -> None:
    """At root level, verified identity revocation closes the
    open session, fences its saved send, clears domain caches, and
    refuses BOTH fresh and saved pre-revocation open/submit handlers
    (guards read live state, never their render's snapshot)."""

    async def scenario() -> None:
        app, controller, sessions, transport = await _render()
        saved_open = app.find(f"agent-{AGENT_KEY}").on_click
        await app.set_props_and_fire(f"agent-{AGENT_KEY}", {}, "click")
        await app.set_props_and_fire("console-new-agent", {}, "click")
        await app.set_props_and_fire("agent-workspace", {"value": "w1"}, "select", "w1")
        await app.set_props_and_fire("agent-name", {"value": "web"}, "change", "web")
        saved_submit = app.find("sheet-submit").on_click
        # The saved submit handler is proven effective pre-revocation
        # (A/B on the identical handler below).
        saved_submit(None)
        await app.settle()
        assert len(transport.submitted) == 1
        await _open_agent(app)
        await app.set_props_and_fire(AGENT_TAKEOVER_CONFIRM_KEY, {}, "click")
        await app.settle()
        session = sessions.factory.created[0]
        assert session.phase == "live"
        session.on_draft_change("do not leak")
        saved_send = session.composer_callbacks()["on_send"]
        pre_revoke = controller.state

        controller._set(reducers.revoke(controller.state))
        await app.settle()

        assert controller.state.revoked is True
        assert controller.state.connection.state == ConnectionState.ACCESS_REVOKED
        assert session.disposed is True
        assert _missing(app, AGENT_DETAIL_KEY)
        assert await saved_send() == "ignored"
        assert sessions.submitted == []
        # Session clearing: the revoked draft is wiped in place.
        assert session.composer_props()["draft"] == ""
        # Domain clearing: no caches survive for the next identity.
        assert controller.state.inventory.hosts == {}
        assert controller.state.inventory.agents == {}
        assert controller.state.selection.host_id == ""
        assert controller.state.granted_hosts is None
        # Saved pre-revocation handlers refuse (no new session, no submit).
        # The saved row handler (selection opens the window) refuses.
        await saved_open(None)
        await app.settle()
        assert len(sessions.factory.created) == 1
        saved_submit(None)
        await app.settle()
        assert len(transport.submitted) == 1
        # Stashed drafts were purged: a later sync under this root
        # restores nothing from the revoked identity.
        controller._set(pre_revoke)
        await app.settle()
        await _open_agent(app)
        await app.settle()
        assert sessions.factory.created[1].composer_props()["draft"] == ""

    run(scenario())


def test_back_and_reopen_preserves_agent_draft_same_identity() -> None:
    """Ordinary Back stashes the draft and reopen restores it:
    purging applies to revocation/logout only, never navigation."""

    async def scenario() -> None:
        app, _controller, sessions, _transport = await _render()
        await _open_agent(app)
        await app.set_props_and_fire(AGENT_TAKEOVER_CONFIRM_KEY, {}, "click")
        await app.settle()
        first = sessions.factory.created[0]
        assert first.phase == "live"
        first.on_draft_change("keep me")
        await app.set_props_and_fire("terminal-back", {}, "click")
        await app.settle()
        assert first.disposed is False and not first.foreground
        assert _missing(app, AGENT_DETAIL_KEY)
        await _open_agent(app)
        await app.settle()
        assert len(sessions.factory.created) == 1 and len(sessions.ops.attaches) == 1
        second = sessions.factory.created[0]
        assert second is first
        assert second.composer_props()["draft"] == "keep me"

    run(scenario())


def test_wire_boundary_carries_bare_upstream_ids() -> None:
    """The boundary through the shipped root: rows/routes/selections
    are host-scoped, but agent_id/target/open/attach carry raw upstream
    ids only — a scoped key (host prefix, quoted separators) must never
    reach the ops wire."""

    async def scenario() -> None:
        app, _controller, sessions, _transport = await _render()
        assert AGENT_KEY != AGENT_ID and SHELL_KEY != SHELL_TAB
        await _open_agent(app)
        await app.set_props_and_fire(AGENT_TAKEOVER_CONFIRM_KEY, {}, "click")
        await app.settle()
        session = sessions.factory.created[0]
        assert session.agent_id == AGENT_ID
        assert session.target == AGENT_ID
        (attach,) = sessions.ops.attaches
        assert attach["host_id"] == "h1"
        assert attach["terminal_id"] == AGENT_ID
        assert attach["terminal_id"] != AGENT_KEY
        await app.set_props_and_fire("terminal-back", {}, "click")
        await app.settle()
        await _open_shell(app)
        shell = sessions.factory.created[1]
        assert shell.terminal_id == SHELL_TAB
        assert sessions.ops.attaches[1]["terminal_id"] == SHELL_TAB
        # SHELL_TAB itself contains ":" — this proves unquoting, not a
        # prefix strip ("h1:w1%3At2" would fail both asserts).
        assert sessions.ops.attaches[1]["terminal_id"] != SHELL_KEY

    run(scenario())
