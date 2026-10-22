"""Automatic control acquisition compares the observation before displacing any owner."""

import asyncio
import json
from typing import Any

import httpx
import pytest

from relaydeck.client.features.shell.ops import HttpxShellGatewayOps
from relaydeck.client.features.terminal.agent_session import (
    AGENT_CONFLICT,
    AGENT_LIVE,
    AgentSession,
)
from tests.support.fakes import FakeClipboard
from tests.support.terminal.lease_gateway import _client, _headers, _token
from tests.support.terminal.sessions import _SocketFactory

PATH = "/v1/hosts/home-mac/terminals/sess:1"


@pytest.mark.parametrize(
    "intervention", ["none", "attach", "lease", "released", "explicit", "after_attach"]
)
def test_holder_race_through_real_agent_and_http(intervention: str) -> None:
    async def scenario() -> None:
        client, state, backend, _clock = _client()
        access_a = _token(state, device="dev-a")
        access_b = _token(state, device="dev-b")
        requests: list[dict[str, Any]] = []

        async def record(request: httpx.Request) -> None:
            if (
                request.url.path.endswith("/attachments")
                and request.method == "POST"
                and request.headers.get("authorization") == f"Bearer {access_a}"
            ):
                requests.append(json.loads(request.content))

        async def token() -> str:
            return access_a

        async def unused(*args: Any) -> dict[str, Any]:
            return {}

        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=client.app),
            base_url="https://gateway.test",
            event_hooks={"request": [record]},
        ) as api:
            ops = HttpxShellGatewayOps("https://gateway.test", token, api)
            session = AgentSession(
                ops=ops,
                host_id="home-mac",
                device_id="dev-a",
                socket_factory=_SocketFactory(),
                submit_command=unused,
                prompt_text=unused,
                host_epoch="epoch-1",
                agent_id="agent-1",
                target="sess:1",
                clipboard=FakeClipboard(),
            )
            original = ops.control_holder
            other: dict[str, Any] = {}

            async def changed(host: str, target: str) -> dict[str, Any]:
                observed = await original(host, target)
                if intervention not in ("none", "after_attach"):
                    attached = await api.post(
                        PATH + "/attachments",
                        headers=_headers(access_b),
                        json={"kind": "agent", "takeover": True},
                    )
                    assert attached.is_success
                    other["attachment"] = attached.json()
                    if intervention != "attach":
                        leased = await api.post(
                            "/v1/terminal-attachments/"
                            + attached.json()["attachment_id"]
                            + "/leases",
                            headers=_headers(access_b),
                            json={"mode": "control"},
                        )
                        assert leased.is_success
                        other["lease"] = leased.json()
                        if intervention == "released":
                            released = await api.delete(
                                "/v1/terminal-leases/" + leased.json()["lease_id"],
                                headers=_headers(access_b),
                            )
                            assert released.is_success
                return observed

            ops.control_holder = changed
            original_attach = ops.attach

            async def after_attach(*args: Any, **kwargs: Any) -> Any:
                result = await original_attach(*args, **kwargs)
                displaced = await api.post(
                    PATH + "/attachments",
                    headers=_headers(access_b),
                    json={"kind": "agent", "takeover": True},
                )
                assert displaced.is_success
                other["attachment"] = displaced.json()
                return result

            if intervention == "after_attach":
                ops.attach = after_attach
            try:
                session.open()
                await session.auto_take_control()
                assert "expected_control_revision" in requests[-1]
                if intervention == "none":
                    assert session.phase == AGENT_LIVE and session.lease.mode == "control"
                else:
                    assert session.phase == AGENT_CONFLICT and session.lease is None
                    attachment = backend.get_attachment(other["attachment"]["attachment_id"])
                    assert attachment.generation == other["attachment"]["generation"]
                    assert attachment.device_id == "dev-b"
                    assert (
                        backend.rings.get(attachment.attachment_id).ring.epoch == attachment.epoch
                    )
                    if intervention not in ("attach", "released", "after_attach"):
                        held = backend.ownership._leases[other["lease"]["lease_id"]]
                        assert held.live and not held.superseded
                    if intervention == "explicit":
                        session.request_takeover()
                        assert await session.confirm_takeover() == AGENT_LIVE
                        assert "expected_control_revision" not in requests[-1]
                        assert held.superseded
            finally:
                await session.dispose()

    asyncio.run(scenario())


def test_holder_race_legacy_force_still_works_and_condition_is_validated() -> None:
    client, state, backend, _clock = _client()
    token = _token(state)
    for invalid in (None, True, 1, "", "z" * 64, "a" * 65):
        reply = client.post(
            PATH + "/attachments",
            headers=_headers(token),
            json={"kind": "agent", "takeover": True, "expected_control_revision": invalid},
        )
        assert reply.status_code == 400
    assert backend.ownership._attachments == {}
    for _ in range(2):
        reply = client.post(
            PATH + "/attachments",
            headers=_headers(token),
            json={"kind": "agent", "takeover": True},
        )
        assert reply.is_success
    assert reply.json()["generation"] == 2
