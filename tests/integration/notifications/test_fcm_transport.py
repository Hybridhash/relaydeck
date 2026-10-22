"""Configured FCM transport and lifespan worker, using synthetic keys/HTTP only."""

import asyncio
import base64
import json
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs

import httpx
import pytest
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa

from relaydeck.gateway.app import create_app
from relaydeck.gateway.cli import build_parser, build_serve_state, resolve_serve_options
from relaydeck.gateway.fcm import FCM_SCOPE, TOKEN_URL, FcmProvider
from relaydeck.gateway.push import HINT_KEYS, ProviderOutcome, PushStore
from relaydeck.gateway.push_worker import PushWorker
from tests.security.helpers import make_state as gateway_state
from tests.support.push import StubProvider
from tests.support.sync.transport_fixtures import _wait


def make_state() -> tuple[Any, Any]:
    state, backend = gateway_state()
    state.push.close()
    state.push = PushStore(clock=state.now_ms)
    state.hosts.enroll("home-mac", account="acct-1")
    state.sessions.create("acct-1", {"sessions.read"}, {"home-mac"}, state.now_ms(), "native-1")
    return state, backend


@pytest.fixture(scope="module")
def credentials() -> dict[str, Any]:
    # Synthetic ephemeral test key, never a Firebase account credential.
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    return {
        "type": "service_account",
        "project_id": "synthetic-project",
        # The transport only accepts the provider's service-account address shape.
        "client_email": "@".join(("test", "synthetic-project.iam.gserviceaccount.com")),
        "private_key": key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        ).decode(),
        "token_uri": TOKEN_URL,
    }


def hint(state: Any) -> dict[str, str]:
    state.push.register_device("acct-1", "native-1", "synthetic-device-token", "android")
    rows = state.push.notify(
        account="acct-1",
        account_hosts={"home-mac"},
        host_id="home-mac",
        session_id="term-private-title",
        event_id="event-private-source-text",
        category="attention",
        now=state.now_ms(),
    )
    return dict(state.push.hint_payload(rows[0]["notification_id"]))


def test_fcm_signs_scoped_oauth_and_sends_only_generic_allowlisted_hints(
    credentials: dict[str, Any],
) -> None:
    state, _ = make_state()
    payload = hint(state)
    sent: list[httpx.Request] = []
    now = [1000.0]

    def response(request: httpx.Request) -> httpx.Response:
        sent.append(request)
        if str(request.url) == TOKEN_URL:
            assertion = parse_qs(request.content.decode())["assertion"][0]
            header, body, signature = assertion.split(".")

            def decode(text: str) -> bytes:
                return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))

            private = serialization.load_pem_private_key(
                credentials["private_key"].encode(), password=None
            )
            assert isinstance(private, rsa.RSAPrivateKey)
            private.public_key().verify(
                decode(signature), f"{header}.{body}".encode(), padding.PKCS1v15(), hashes.SHA256()
            )
            claims = json.loads(decode(body))
            assert claims["aud"] == TOKEN_URL and claims["scope"] == FCM_SCOPE
            assert claims["exp"] - claims["iat"] == 3600
            return httpx.Response(
                200,
                json={
                    "access_token": "synthetic-access",
                    "token_type": "Bearer",
                    "expires_in": 3600,
                },
            )
        assert request.url.host == "fcm.googleapis.com"
        assert request.headers["authorization"] == "Bearer synthetic-access"
        message = json.loads(request.content)["message"]
        assert set(message["data"]) == HINT_KEYS and message["data"] == payload
        assert "private-source-text" not in request.content.decode()
        assert "private-title" not in request.content.decode()
        assert message["notification"] == {
            "title": "RelayDeck",
            "body": "Open RelayDeck to view the update.",
        }
        return httpx.Response(200, json={"name": "projects/synthetic-project/messages/synthetic"})

    try:
        with httpx.Client(transport=httpx.MockTransport(response)) as client:
            provider = FcmProvider(credentials, client=client, clock=lambda: now[0])
            assert provider.send("synthetic-device-token", payload) == ProviderOutcome.SENT
            assert provider.send("synthetic-device-token", payload) == ProviderOutcome.SENT
            assert sum(str(req.url) == TOKEN_URL for req in sent) == 1
            now[0] += 3550
            assert provider.send("synthetic-device-token", payload) == ProviderOutcome.SENT
            assert sum(str(req.url) == TOKEN_URL for req in sent) == 2
            provider.close()
    finally:
        state.push.close()


@pytest.mark.parametrize(
    "status,code,outcome",
    [
        (404, "UNREGISTERED", ProviderOutcome.PERMANENT),
        (400, "INVALID_ARGUMENT", ProviderOutcome.RETRYABLE),
        (403, "SENDER_ID_MISMATCH", ProviderOutcome.RETRYABLE),
        (429, "QUOTA_EXCEEDED", ProviderOutcome.RETRYABLE),
        (503, "UNAVAILABLE", ProviderOutcome.RETRYABLE),
        (401, "", ProviderOutcome.RETRYABLE),
    ],
)
def test_only_explicit_unregistered_tokens_are_tombstoned(
    credentials: dict[str, Any], status: int, code: str, outcome: ProviderOutcome
) -> None:
    state, _ = make_state()
    payload = hint(state)

    def response(request: httpx.Request) -> httpx.Response:
        if str(request.url) == TOKEN_URL:
            return httpx.Response(
                200, json={"access_token": "synthetic-access", "expires_in": 3600}
            )
        return httpx.Response(
            status,
            json={
                "error": {
                    "details": [
                        {
                            "@type": "type.googleapis.com/google.firebase.fcm.v1.FcmError",
                            "errorCode": code,
                        }
                    ]
                }
            },
        )

    try:
        with httpx.Client(transport=httpx.MockTransport(response)) as client:
            provider = FcmProvider(credentials, client=client)
            assert provider.send("synthetic-device-token", payload) == outcome
            provider.close()
    finally:
        state.push.close()


def test_fcm_configuration_rejects_foreign_token_authority_and_public_or_symlink_files(
    credentials: dict[str, Any], tmp_path: Path
) -> None:
    with pytest.raises(ValueError):
        FcmProvider({**credentials, "token_uri": "https://foreign.example/token"})
    file = tmp_path / "synthetic.json"
    file.write_text(json.dumps(credentials))
    file.chmod(0o644)
    with pytest.raises(ValueError):
        FcmProvider.from_file(file)
    file.chmod(0o600)
    link = tmp_path / "link.json"
    link.symlink_to(file)
    with pytest.raises(ValueError):
        FcmProvider.from_file(link)
    args = build_parser().parse_args(
        ["serve", "--fcm-credentials", str(file), "--push-path", str(tmp_path / "push.db")]
    )
    state = build_serve_state(resolve_serve_options(args))
    assert isinstance(state.push_provider, FcmProvider)
    state.push_provider.close()
    state.push.close()


def test_gateway_lifespan_owns_one_worker_and_disabled_provider_sends_nothing() -> None:
    async def scenario() -> None:
        state, _ = make_state()
        hint(state)
        app = create_app(state)
        async with app.router.lifespan_context(app):
            assert app.state.push_worker is None and state.push.pending_count() == 1
        provider = StubProvider()
        state.push_provider = provider
        app = create_app(state)
        async with app.router.lifespan_context(app):
            worker = app.state.push_worker
            assert isinstance(worker, PushWorker)
            task = worker.task
            worker.start()
            assert task is worker.task
            await _wait(lambda: len(provider.sends) == 1)
        assert task is not None and task.done() and state.push.pending_count() == 0
        state.push.close()

    asyncio.run(scenario())


def test_shutdown_waits_for_inflight_send_then_stops_remaining_batch() -> None:
    import threading

    async def scenario() -> None:
        state, _ = make_state()
        hint(state)
        state.push.notify(
            account="acct-1",
            account_hosts={"home-mac"},
            host_id="home-mac",
            session_id="term-2",
            event_id="event-2",
            category="completion",
            now=state.now_ms(),
        )
        entered, release = threading.Event(), threading.Event()

        class Held(StubProvider):
            def send(self, token: str, payload: dict[str, str]) -> ProviderOutcome:
                entered.set()
                assert release.wait(2)
                return super().send(token, payload)

        provider = Held()
        worker = PushWorker(state.push, provider, interval=0.01)
        worker.start()
        await _wait(entered.is_set)
        stop = asyncio.create_task(worker.stop())
        await asyncio.sleep(0)
        assert not stop.done()
        release.set()
        await stop
        assert len(provider.sends) == 1 and state.push.pending_count() == 1
        state.push.close()

    asyncio.run(scenario())
