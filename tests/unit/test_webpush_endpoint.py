"""Endpoint allowlist, public-address check, provider path.

No network: DNS is stubbed and httpx uses a mock transport.
"""

from __future__ import annotations

import json
import logging
from typing import Any

import httpx
import pytest
from cryptography.hazmat.primitives.asymmetric import ec

from relaydeck.gateway import webpush
from relaydeck.gateway.push import ProviderOutcome

GOOD = "https://fcm.googleapis.com/fcm/send/abc123"

ALLOWED = [
    GOOD,
    "https://web.push.apple.com/QOabc",
    "https://updates.push.services.mozilla.com/wpush/v2/abc",
    "https://wns2-db5p.notify.windows.com/w/?token=abc",
]

REJECTED = [
    # scheme and port
    "http://fcm.googleapis.com/fcm/send/abc",
    "https://fcm.googleapis.com:8443/fcm/send/abc",
    "ftp://fcm.googleapis.com/x",
    # userinfo and fragments
    "https://fcm.googleapis.com:443@127.0.0.1/x",
    "https://user:pw@127.0.0.1/x",
    "https://fcm.googleapis.com/x#frag",
    # IP literals and loopback names
    "https://127.0.0.1/x",
    "https://[::1]/x",
    "https://10.0.0.5/x",
    "https://localhost/x",
    # suffix attacks: exact hosts only, label boundary for the wildcard suffix
    "https://evilfcm.googleapis.com/x",
    "https://fcm.googleapis.com.evil.example/x",
    "https://evilnotify.windows.com/x",
    "https://notify.windows.com.evil.example/x",
    "https://notify.windows.com/x",
    "https://example.com/x",
    # IDNA: a lookalike that encodes to a non-allowlisted name
    "https://fcm.googleapis.com․evil.example/x",
    "https://fcm。googleapis.com.evil.example/x",
    "",
    "not a url",
]


@pytest.mark.parametrize("url", ALLOWED)
def test_allowlisted_hosts_pass(url: str) -> None:
    target = webpush.check_endpoint(url)
    assert target.host in webpush.EXACT_HOSTS or target.host.endswith(".notify.windows.com")


@pytest.mark.parametrize("url", REJECTED)
def test_everything_else_is_rejected_without_detail(url: str) -> None:
    with pytest.raises(webpush.EndpointRejected) as caught:
        webpush.check_endpoint(url)
    if url:
        assert url not in str(caught.value)  # the refusal never echoes the endpoint


def test_the_host_is_canonicalised_before_comparison() -> None:
    assert webpush.check_endpoint("https://FCM.GoogleAPIs.com/x").host == "fcm.googleapis.com"


def test_the_path_and_query_are_kept_for_the_request() -> None:
    target = webpush.check_endpoint("https://fcm.googleapis.com/fcm/send/abc?x=1")
    assert target.path == "/fcm/send/abc?x=1"


def test_a_private_resolution_is_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    for address in ("10.0.0.7", "127.0.0.1", "169.254.1.1", "192.168.1.9", "100.64.0.1", "::1"):
        monkeypatch.setattr(
            webpush.socket,
            "getaddrinfo",
            lambda *_a, _addr=address, **_k: [(2, 1, 6, "", (_addr, 443))],
        )
        with pytest.raises(webpush.EndpointRejected):
            webpush.resolve_public("fcm.googleapis.com")


def test_one_private_address_among_public_ones_still_fails_closed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        webpush.socket,
        "getaddrinfo",
        lambda *_a, **_k: [
            (2, 1, 6, "", ("142.250.0.10", 443)),
            (2, 1, 6, "", ("10.0.0.7", 443)),
        ],
    )
    with pytest.raises(webpush.EndpointRejected):
        webpush.resolve_public("fcm.googleapis.com")


def test_a_public_resolution_returns_the_validated_address(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        webpush.socket,
        "getaddrinfo",
        lambda *_a, **_k: [(2, 1, 6, "", ("142.250.0.10", 443))],
    )
    assert webpush.resolve_public("fcm.googleapis.com") == "142.250.0.10"


def test_a_resolution_failure_is_retryable_not_permanent(monkeypatch: pytest.MonkeyPatch) -> None:
    import socket

    def fail(*_a: Any, **_k: Any) -> None:
        raise socket.gaierror("no such host")

    monkeypatch.setattr(webpush.socket, "getaddrinfo", fail)
    key = ec.generate_private_key(ec.SECP256R1())
    provider = webpush.WebPushProvider(
        key, client=httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(201)))
    )
    assert (
        provider.send(_token(), {"t": "RelayDeck", "b": "x", "r": "wp_x"})
        is ProviderOutcome.RETRYABLE
    )


def _token(endpoint: str = GOOD, **overrides: Any) -> str:
    sub: dict[str, Any] = {
        "endpoint": endpoint,
        "p256dh": webpush.b64u(b"\x04" + b"\x01" * 64),
        "auth": webpush.b64u(b"\x02" * 16),
        "ttl": 60,
        "urgency": "high",
        "topic": "topic-abc",
    }
    sub.update(overrides)
    return json.dumps(sub)


def _ua_key() -> tuple[str, str]:
    """A real subscriber key pair so the provider's encryption succeeds in tests."""
    from cryptography.hazmat.primitives import serialization

    priv = ec.generate_private_key(ec.SECP256R1())
    pub = priv.public_key().public_bytes(
        serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint
    )
    return webpush.b64u(pub), webpush.b64u(b"\x03" * 16)


def _provider(
    handler: Any, *, resolver: Any = lambda host: "142.250.0.10"
) -> webpush.WebPushProvider:
    key = ec.generate_private_key(ec.SECP256R1())
    client = httpx.Client(transport=httpx.MockTransport(handler), follow_redirects=False)
    return webpush.WebPushProvider(
        key,
        client=client,
        clock=lambda: 1_700_000_000,
        resolver=resolver,
    )


def _token_real(endpoint: str = GOOD, **overrides: Any) -> str:
    p256dh, auth = _ua_key()
    doc = json.loads(_token(endpoint))
    doc.update({"p256dh": p256dh, "auth": auth})
    doc.update(overrides)
    return json.dumps(doc)


def test_a_successful_send_connects_to_the_validated_address_with_sni_and_no_redirects() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(201)

    provider = _provider(handler)
    outcome = provider.send(
        _token_real(), {"t": "RelayDeck", "b": "An agent needs attention", "r": "wp_x"}
    )
    assert outcome is ProviderOutcome.SENT
    request = seen[0]
    assert request.url.host == "142.250.0.10"  # the validated address
    assert request.headers["host"] == "fcm.googleapis.com"
    assert request.extensions["sni_hostname"] == "fcm.googleapis.com"
    assert request.headers["content-encoding"] == "aes128gcm"
    assert request.headers["ttl"] == "60"
    assert request.headers["urgency"] == "high"
    assert request.headers["topic"] == "topic-abc"
    assert request.headers["authorization"].startswith("vapid t=")
    assert ", k=" in request.headers["authorization"]


def test_the_vapid_header_carries_an_es256_jwt_for_the_push_origin() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(201)

    _provider(handler).send(_token_real(), {"t": "RelayDeck", "b": "x", "r": "wp_x"})
    header = seen[0].headers["authorization"]
    token = header.split("t=", 1)[1].split(",", 1)[0]
    claims = json.loads(webpush.b64u_decode(token.split(".")[1]))
    assert claims["aud"] == "https://fcm.googleapis.com"
    assert claims["sub"] == "https://example.com/relaydeck"
    assert claims["exp"] - 1_700_000_000 <= 24 * 60 * 60


def test_the_send_is_refused_before_any_connection_for_a_bad_endpoint() -> None:
    calls: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(201)

    provider = _provider(handler)
    assert (
        provider.send(_token_real("http://fcm.googleapis.com/x"), {"t": "a", "b": "b", "r": "c"})
        is ProviderOutcome.PERMANENT
    )
    assert (
        provider.send(
            _token_real("https://evilfcm.googleapis.com/x"), {"t": "a", "b": "b", "r": "c"}
        )
        is ProviderOutcome.PERMANENT
    )
    assert calls == []


def test_a_private_resolution_refuses_the_send_without_connecting() -> None:
    calls: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(201)

    def private(host: str) -> str:
        raise webpush.EndpointRejected("non-public-address")

    provider = _provider(handler, resolver=private)
    assert provider.send(_token_real(), {"t": "a", "b": "b", "r": "c"}) is ProviderOutcome.PERMANENT
    assert calls == []


@pytest.mark.parametrize(
    ("status", "expected"),
    [
        (201, ProviderOutcome.SENT),
        (200, ProviderOutcome.SENT),
        (404, ProviderOutcome.PERMANENT),
        (410, ProviderOutcome.PERMANENT),
        (429, ProviderOutcome.RETRYABLE),
        (500, ProviderOutcome.RETRYABLE),
        (400, ProviderOutcome.RETRYABLE),
    ],
)
def test_provider_statuses_map_to_outcomes(status: int, expected: ProviderOutcome) -> None:
    provider = _provider(lambda request: httpx.Response(status))
    assert provider.send(_token_real(), {"t": "a", "b": "b", "r": "c"}) is expected


def test_a_redirect_is_never_followed() -> None:
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(str(request.url))
        return httpx.Response(302, headers={"location": "https://evil.example/"})

    outcome = _provider(handler).send(_token_real(), {"t": "a", "b": "b", "r": "c"})
    assert outcome is ProviderOutcome.RETRYABLE
    assert len(seen) == 1


def test_an_oversize_response_is_read_only_up_to_the_cap() -> None:
    body_sizes: list[int] = []

    class Big(httpx.SyncByteStream):
        def __iter__(self):  # type: ignore[no-untyped-def]
            for _ in range(1000):
                yield b"x" * 1024
                body_sizes.append(1)

    provider = _provider(lambda request: httpx.Response(500, stream=Big()))
    assert provider.send(_token_real(), {"t": "a", "b": "b", "r": "c"}) is ProviderOutcome.RETRYABLE
    assert len(body_sizes) <= webpush.RESPONSE_CAP_BYTES // 1024 + 1


def test_an_oversize_payload_is_never_sent() -> None:
    calls: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(201)

    provider = _provider(handler)
    assert provider.send(_token_real(), {"b": "x" * 3000}) is ProviderOutcome.PERMANENT
    assert calls == []


def test_a_transport_error_is_retryable_and_logs_no_endpoint_or_keys(
    caplog: pytest.LogCaptureFixture,
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused to " + str(request.url))

    caplog.set_level(logging.DEBUG)
    outcome = _provider(handler).send(_token_real(), {"t": "a", "b": "b", "r": "c"})
    assert outcome is ProviderOutcome.RETRYABLE
    assert len(caplog.records) == 1  # one WARNING per failed send
    message = caplog.records[0].getMessage()
    assert "host=fcm.googleapis.com" in message and "reason=ConnectError" in message
    assert "/fcm/send" not in message and "synthetic" not in message and GOOD not in caplog.text


def test_the_default_client_has_a_ten_second_timeout_and_no_redirects(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    built: list[dict[str, Any]] = []
    real = httpx.Client

    def recording(**kwargs: Any) -> httpx.Client:
        built.append(kwargs)
        return real(transport=httpx.MockTransport(lambda r: httpx.Response(201)), **kwargs)

    monkeypatch.setattr(webpush.httpx, "Client", recording)
    provider = webpush.WebPushProvider(ec.generate_private_key(ec.SECP256R1()))
    assert built[0]["timeout"] == 10.0
    assert built[0]["follow_redirects"] is False
    assert webpush.TIMEOUT_SECONDS == 10.0
    provider.close()


def test_the_ten_second_timeout_reaches_each_outgoing_request(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(dict(request.extensions["timeout"]))
        return httpx.Response(201)

    real = httpx.Client

    def with_transport(**kwargs: Any) -> httpx.Client:
        return real(transport=httpx.MockTransport(handler), **kwargs)

    monkeypatch.setattr(webpush.httpx, "Client", with_transport)
    provider = webpush.WebPushProvider(
        ec.generate_private_key(ec.SECP256R1()),
        resolver=lambda host: "142.250.0.10",
        clock=lambda: 1_700_000_000,
    )
    assert provider.send(_token_real(), {"t": "a", "b": "b", "r": "c"}) is ProviderOutcome.SENT
    assert seen == [{"connect": 10.0, "read": 10.0, "write": 10.0, "pool": 10.0}]
    provider.close()
