"""Native lifecycle actor over real auth/gateway; terminal and native services are doubles."""

from typing import Any

import flet as ft
import httpx
import pytest

from relaydeck.client.features.auth.service import REFRESH_KEY
from relaydeck.client.packaged_runtime import PackagedRuntime, ScopedDrafts
from relaydeck.domain.models import scope_key
from relaydeck.gateway.app import create_app
from tests.security.helpers import ALL_CAPS, NOW, make_state
from tests.support.fakes import FakeClipboard, FakeCredentialStore
from tests.support.host_harness import render_component, run
from tests.support.sync.lifecycle import _agent
from tests.support.ui.packaged import Preferences


@pytest.mark.parametrize("failure", ["none", "snapshot", "revoked", "superseded"])
def test_resume_requires_current_identity_and_snapshot_without_replaying_pending_commands(
    failure: str,
    monkeypatch: Any,
) -> None:
    async def scenario() -> None:
        state, backend = make_state()
        backend.snapshots["home-mac"]["agents"][0]["terminal_id"] = "agent-1"
        _, refresh = state.sessions.create(
            "acct-1", ALL_CAPS, frozenset({"home-mac"}), NOW, "native-login"
        )
        secure = FakeCredentialStore()
        await secure.set(REFRESH_KEY, refresh)
        await secure.set("identity_origin", "https://gw")
        prefs = Preferences({"relaydeck-endpoint": "https://gw"})
        requests: list[str] = []

        async def requested(request: httpx.Request) -> None:
            requests.append(f"{request.method} {request.url.path}")

        async def browser(url: str) -> None:
            pytest.fail("Resume must not launch identity UI silently")

        app = await render_component(lambda: ft.Text("Host harness"))
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=create_app(state)),
            event_hooks={"request": [requested]},
        ) as http:
            runtime = PackagedRuntime(
                app.page,
                secure,
                prefs,
                browser,
                lambda: None,
                client=http,
                clipboard=FakeClipboard(),
            )
            try:
                await runtime.start()
                assert runtime.resources is not None
                assert runtime.resources is not None
                resources = runtime.resources
                drafts = ScopedDrafts(secure, "acct-1")
                session, ops, _, submitted = _agent(draft_store=drafts)
                session.open()
                await session.confirm_takeover()
                session.on_draft_change("synthetic unsent work")
                session.store._last_command_id = "uncertain-command"
                from relaydeck.client.features.composer.commands import agent_prompt_command

                await resources.commands.begin(
                    agent_prompt_command(
                        command_id="uncertain-command",
                        host_id="home-mac",
                        host_epoch="epoch-7",
                        agent_id="agent-1",
                        text="not dispatched",
                    )
                )
                await resources.retained.acquire(
                    "home-mac",
                    "agent",
                    scope_key("home-mac", "agent-1"),
                    "epoch-7",
                    lambda: session,
                )
                previous_attach_count = len(ops.attaches)
                runtime.background()
                assert (
                    session.paused and session.input_token is None and not runtime.input_allowed()
                )
                assert runtime._pause_task is not None
                await runtime._pause_task
                assert "synthetic unsent work" not in str(prefs.values)
                if failure == "revoked":
                    state.sessions.revoke_device("acct-1", "native-login")
                elif failure == "snapshot":

                    async def unavailable(host: str) -> dict[str, Any]:
                        raise httpx.ReadError("synthetic private error")

                    monkeypatch.setattr(resources.transport, "snapshot", unavailable)
                elif failure == "superseded":
                    snapshot = resources.transport.snapshot

                    async def delayed(host: str) -> dict[str, Any]:
                        runtime.background()
                        return await snapshot(host)

                    monkeypatch.setattr(resources.transport, "snapshot", delayed)
                runtime.foreground = True
                await runtime.resume()
                assert submitted == [] and not any(
                    "POST /v1/commands" in request for request in requests
                )
                assert len(ops.attaches) == previous_attach_count
                if failure == "none":
                    assert runtime.input_allowed() and runtime.resume_report is not None
                    assert runtime.resume_report.pending == {
                        repr(("home-mac", "uncertain-command")): "unknown"
                    }
                    assert not runtime.resume_report.control_granted
                    assert session.takeover_disclosure is not None and session.lease is None
                    assert session.composer_props()["draft"] == "synthetic unsent work"
                    replacement, _, _, _ = _agent(draft_store=drafts)
                    await replacement.prepare_draft()
                    assert replacement.composer_props()["draft"] == "synthetic unsent work"
                    foreign, _, _, _ = _agent(draft_store=ScopedDrafts(secure, "acct-2"))
                    await foreign.prepare_draft()
                    assert foreign.composer_props()["draft"] == ""
                    await replacement.dispose(clear_sensitive=True)
                    await foreign.dispose(clear_sensitive=True)
                else:
                    assert not runtime.input_allowed()
                    if failure == "revoked":
                        assert runtime.phase == "login" and runtime.resources is None
                    elif failure == "snapshot":
                        assert runtime.phase == "error" and session.paused
                    else:
                        assert runtime.phase == "paused" and session.paused
            finally:
                await runtime.close()
                state.push.close()

    run(scenario())
