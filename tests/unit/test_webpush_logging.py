"""operator logging and the VAPID subject rule (after the live BadJwtToken report).

One WARNING per failed send: the provider host, the HTTP status and the provider's short reason
word. Never an endpoint path, key, JWT or payload. Mocked transport only; no network.
"""

from __future__ import annotations

import json
import logging
from typing import Any

import httpx
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec

from relaydeck.companion.config import NotificationsConfig, notifications_from, valid_vapid_subject
from relaydeck.gateway import webpush
from relaydeck.gateway.push import ProviderOutcome

APPLE = "https://web.push.apple.com/QOSynthetic-path-SECRET-7f3a"
SYNTHETIC_PAYLOAD = {"t": "RelayDeck", "b": "An agent needs attention", "r": "wp_synthetic_route_1"}


def _keys() -> tuple[str, str]:
    priv = ec.generate_private_key(ec.SECP256R1())
    pub = priv.public_key().public_bytes(
        serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint
    )
    return webpush.b64u(pub), webpush.b64u(b"\x04" * 16)


def _token(endpoint: str) -> str:
    p256dh, auth = _keys()
    return json.dumps(
        {
            "endpoint": endpoint,
            "p256dh": p256dh,
            "auth": auth,
            "ttl": 60,
            "urgency": "high",
            "topic": "synthetic-topic",
        }
    )


def _provider(handler: Any) -> webpush.WebPushProvider:
    client = httpx.Client(transport=httpx.MockTransport(handler), follow_redirects=False)
    return webpush.WebPushProvider(
        ec.generate_private_key(ec.SECP256R1()),
        client=client,
        clock=lambda: 1_700_000_000,
        resolver=lambda host: "17.250.0.1",
    )


def _warnings(caplog: pytest.LogCaptureFixture) -> list[str]:
    return [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]


def test_a_rejected_apple_send_logs_one_warning_with_host_status_and_reason(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.DEBUG)

    def apple(request: httpx.Request) -> httpx.Response:
        return httpx.Response(403, json={"reason": "BadJwtToken"})

    outcome = _provider(apple).send(_token(APPLE), SYNTHETIC_PAYLOAD)
    assert outcome is ProviderOutcome.RETRYABLE
    messages = _warnings(caplog)
    assert len(messages) == 1
    assert "host=web.push.apple.com status=403 reason=BadJwtToken" in messages[0]


def test_the_warning_never_carries_the_path_keys_jwt_or_payload(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.DEBUG)
    seen: list[httpx.Request] = []

    def apple(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(403, json={"reason": "BadJwtToken"})

    token = _token(APPLE)
    _provider(apple).send(token, SYNTHETIC_PAYLOAD)
    everything = "\n".join([*_warnings(caplog), *(r.getMessage() for r in caplog.records)])
    jwt = seen[0].headers["authorization"]
    assert "QOSynthetic-path-SECRET" not in everything
    assert "/QOSynthetic" not in everything
    assert "vapid" not in everything.lower() and jwt not in everything
    assert "synthetic-topic" not in everything and "wp_synthetic_route_1" not in everything
    assert "An agent needs attention" not in everything
    assert all(value not in everything for value in (_keys()[0], _keys()[1], seen[0].url.path))


@pytest.mark.parametrize(
    ("body", "reason"),
    [
        ({"reason": "BadJwtToken"}, "BadJwtToken"),
        ({"reason": "Gone"}, "Gone"),
        ({"reason": "has a space"}, "-"),
        ({"reason": "path/../../secret"}, "-"),
        ({"reason": "x" * 33}, "-"),
        ({"reason": 7}, "-"),
        ({}, "-"),
    ],
)
def test_only_a_short_word_reason_is_reported(
    caplog: pytest.LogCaptureFixture, body: dict[str, Any], reason: str
) -> None:
    caplog.set_level(logging.DEBUG)
    _provider(lambda request: httpx.Response(403, json=body)).send(_token(APPLE), SYNTHETIC_PAYLOAD)
    assert f"reason={reason}" in _warnings(caplog)[0]


def test_a_non_json_body_reports_no_reason(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.DEBUG)
    _provider(lambda request: httpx.Response(500, text="<html>upstream</html>")).send(
        _token(APPLE), SYNTHETIC_PAYLOAD
    )
    assert "reason=-" in _warnings(caplog)[0]
    assert "upstream" not in _warnings(caplog)[0]


def test_a_gone_subscription_is_logged_as_permanent(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.DEBUG)
    outcome = _provider(lambda request: httpx.Response(410)).send(_token(APPLE), SYNTHETIC_PAYLOAD)
    assert outcome is ProviderOutcome.PERMANENT
    assert "status=410" in _warnings(caplog)[0] and "outcome=permanent" in _warnings(caplog)[0]


def test_a_success_logs_nothing(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.DEBUG)
    outcome = _provider(lambda request: httpx.Response(201)).send(_token(APPLE), SYNTHETIC_PAYLOAD)
    assert outcome is ProviderOutcome.SENT
    assert _warnings(caplog) == []


# -- the subject rule ----------------------------------------------------------------------------


@pytest.mark.parametrize(
    "subject",
    [
        "https://example.com/relaydeck",
        "https://push.example.org/contact",
    ],
)
def test_routable_subjects_are_accepted(subject: str) -> None:
    assert valid_vapid_subject(subject)


@pytest.mark.parametrize(
    "subject",
    [
        "https://host.local/x",
        "https://host.test/x",
        "https://LOCALHOST.INVALID/x",
        "https://example/x",
        "https://example.com/a b",
        "https://push.example.invalid/x",
        "https://localhost/x",
        "https://user@127.0.0.1/x",
        "http://push.example.org/x",
        "ftp://push.example.org/x",
        "",
        None,
        7,
    ],
)
def test_placeholder_and_malformed_subjects_are_refused(subject: object) -> None:
    assert valid_vapid_subject(subject) is False


def test_the_default_is_the_reserved_example_domain_and_loads() -> None:
    assert webpush.VAPID_SUBJECT_DEFAULT == "https://example.com/relaydeck"
    assert notifications_from(None) == NotificationsConfig()
    assert valid_vapid_subject(webpush.VAPID_SUBJECT_DEFAULT)


def test_the_configuration_refuses_a_placeholder_subject() -> None:
    with pytest.raises(ValueError):
        notifications_from({"vapid_subject": "https://push.example.invalid/x"})


def test_the_provider_refuses_a_placeholder_subject_at_construction() -> None:
    with pytest.raises(ValueError):
        webpush.WebPushProvider(
            ec.generate_private_key(ec.SECP256R1()),
            subject="https://push.example.invalid/x",
        )
