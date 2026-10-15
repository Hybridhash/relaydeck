"""policy, identity, cursor, and wire codec unit tests."""

from __future__ import annotations

import pytest

from relaydeck.gateway import terminal_wire
from relaydeck.gateway.cursors import (
    CursorError,
    decode_cursor,
    encode_cursor,
)
from relaydeck.gateway.identity import (
    NonceStore,
    SessionStore,
    pkce_challenge,
    pkce_verify,
)
from relaydeck.gateway.policy import (
    authorize_command,
    command_limiter,
    input_limiter,
    is_trusted_endpoint,
)

CAPS = frozenset({"sessions.read", "terminal.view", "terminal.control", "agents.prompt"})
HOSTS = frozenset({"home-mac"})


PROMPT = {"agent_id": "term-1"}
START = {"workspace_id": "w1"}
CLOSE = {"terminal_id": "term-1"}


def test_authorize_command_matrix() -> None:
    assert (
        authorize_command(CAPS, HOSTS, "agent.prompt", "home-mac", PROMPT, {"text": "hi"}) is None
    )
    assert (
        authorize_command(frozenset(), HOSTS, "agent.prompt", "home-mac", PROMPT, {"text": "hi"})
        == "unsupported_capability"
    )
    assert (
        authorize_command(CAPS, HOSTS, "agent.prompt", "other", PROMPT, {"text": "hi"})
        == "not_found"
    )
    assert (
        authorize_command(CAPS, HOSTS, "agent.prompt", "home-mac", PROMPT, {"text": "  "})
        == "forbidden"
    )
    assert (
        authorize_command(CAPS, HOSTS, "agent.prompt", "home-mac", PROMPT, {"text": "x" * 20000})
        == "too_large"
    )
    assert (
        authorize_command(
            CAPS, HOSTS, "agent.prompt", "home-mac", PROMPT, {"text": "hi", "extra": 1}
        )
        == "forbidden"
    )
    assert (
        authorize_command(CAPS, HOSTS, "agent.prompt", "home-mac", {}, {"text": "hi"})
        == "forbidden"
    )
    assert (
        authorize_command(
            CAPS, HOSTS, "agent.prompt", "home-mac", {"terminal_id": "t"}, {"text": "hi"}
        )
        == "forbidden"
    )
    full_evil = CAPS | {"agents.start"}
    assert (
        authorize_command(
            full_evil, HOSTS, "agent.start", "home-mac", START, {"kind": "Evil Kind", "name": "n"}
        )
        == "forbidden"
    )
    assert (
        authorize_command(
            CAPS, HOSTS, "agent.start", "home-mac", START, {"kind": "agent-a", "name": "n"}
        )
        == "unsupported_capability"
    )
    full = CAPS | {"agents.start", "shells.create"}
    assert (
        authorize_command(
            full, HOSTS, "agent.start", "home-mac", START, {"kind": "agent-a", "name": "n"}
        )
        is None
    )
    assert (
        authorize_command(
            full, HOSTS, "agent.start", "home-mac", {}, {"kind": "agent-a", "name": "n"}
        )
        == "forbidden"
    )
    assert (
        authorize_command(
            full,
            HOSTS,
            "agent.start",
            "home-mac",
            START,
            {"kind": "agent-a", "name": "n", "argv": ["--model", "o3"]},
        )
        is None
    )
    assert (
        authorize_command(
            full,
            HOSTS,
            "agent.start",
            "home-mac",
            START,
            {"kind": "agent-a", "name": "n", "argv": "oops"},
        )
        == "forbidden"
    )
    assert (
        authorize_command(
            full,
            HOSTS,
            "agent.start",
            "home-mac",
            START,
            {"kind": "agent-a", "name": "n", "argv": ["x"] * 33},
        )
        == "forbidden"
    )
    assert (
        authorize_command(
            full,
            HOSTS,
            "agent.start",
            "home-mac",
            START,
            {"kind": "agent-a", "name": "n", "worktree": "/tmp"},
        )
        == "forbidden"
    )
    assert authorize_command(full, HOSTS, "shell.create", "home-mac", START, {}) is None
    assert authorize_command(full, HOSTS, "shell.create", "home-mac", {}, {}) == "forbidden"
    assert (
        authorize_command(full, HOSTS, "shell.create", "home-mac", START, {"label": "dev"}) is None
    )
    assert (
        authorize_command(full, HOSTS, "shell.create", "home-mac", {}, {"extra": 1}) == "forbidden"
    )
    assert (
        authorize_command(
            full, HOSTS, "shell.create", "home-mac", {}, {"workspace_id": "w1", "label": "dev"}
        )
        == "forbidden"
    )
    assert authorize_command(CAPS, HOSTS, "session.close", "home-mac", CLOSE, {}) is None
    assert (
        authorize_command(CAPS, HOSTS, "session.close", "home-mac", CLOSE, {"x": 1}) == "forbidden"
    )
    # agent.approve is not a command kind (approval is terminal input), so
    # it probes unknown-kind rejection.
    assert (
        authorize_command(CAPS, HOSTS, "agent.approve", "home-mac", PROMPT, {})
        == "unsupported_capability"
    )


def test_trust_boundaries() -> None:
    assert is_trusted_endpoint("https://gw.example/", frozenset()) is True
    assert is_trusted_endpoint("wss://gw.example/x", frozenset()) is True
    assert is_trusted_endpoint("http://localhost:8000/", frozenset()) is True
    assert is_trusted_endpoint("http://127.0.0.1/", frozenset()) is True
    assert is_trusted_endpoint("http://gw.example/", frozenset()) is False
    assert is_trusted_endpoint("http://gw.example/", frozenset({"gw.example"})) is True
    assert is_trusted_endpoint("ftp://gw.example/", frozenset()) is False
    assert is_trusted_endpoint("not a url", frozenset()) is False


def test_command_rate_limit() -> None:
    limiter = command_limiter()
    for _ in range(20):
        assert limiter.check("a", 0) is True
    assert limiter.check("a", 0) is False
    assert limiter.check("a", 60_001) is True
    assert limiter.check("b", 0) is True


def test_input_rate_limit_bytes() -> None:
    limiter = input_limiter()
    assert limiter.check("l", 0, cost=100 * 1024) is True
    assert limiter.check("l", 0, cost=1) is False
    assert limiter.check("l", 1001, cost=1) is True


def test_pkce_round_trip() -> None:
    verifier = "v" * 64
    challenge = pkce_challenge(verifier)
    assert pkce_verify(verifier, challenge) is True
    assert pkce_verify("w" * 64, challenge) is False
    assert pkce_verify("", challenge) is False


def test_nonce_single_use_and_expiry() -> None:
    store = NonceStore()
    nonce = store.issue("oauth", 1000)
    assert store.claim(nonce, "oauth", 1001) is True
    assert store.claim(nonce, "oauth", 1002) is False
    other = store.issue("oauth", 1000, ttl_ms=10)
    assert store.claim(other, "oauth", 2000) is False
    scoped = store.issue("enroll", 1000)
    assert store.claim(scoped, "oauth", 1001) is False


def test_sessions_lifecycle() -> None:
    store = SessionStore()
    access, refresh = store.create("acct", CAPS, HOSTS, 0)
    assert store.lookup(access, 1) is not None
    assert store.lookup(access, 15 * 60 * 1000 + 1) is None
    pair = store.refresh(refresh, 1000)
    assert pair is not None
    new_access, new_refresh = pair
    assert store.lookup(new_access, 1001) is not None
    assert store.refresh(refresh, 1002) is None  # rotated, no reuse
    assert store.revoke_account("acct") == 1
    assert store.lookup(new_access, 1003) is None
    assert store.refresh(new_refresh, 1004) is None


def test_cursor_round_trip_and_tamper() -> None:
    secret = b"test-secret-32-bytes-long-ok!!"
    cursor = encode_cursor(secret, {"host_id": "h", "sequence": 42})
    assert decode_cursor(secret, cursor) == {"host_id": "h", "sequence": 42}
    head, dot, tail = cursor.partition(".")
    tampered = _b64({"host_id": "evil", "sequence": 42}) + dot + tail
    with pytest.raises(CursorError):
        decode_cursor(secret, tampered)
    with pytest.raises(CursorError):
        decode_cursor(b"other-secret-32-bytes-long-ok!", cursor)
    with pytest.raises(CursorError):
        decode_cursor(secret, "not-a-cursor")
    with pytest.raises(CursorError):
        encode_cursor(b"", {"a": 1})


def _b64(payload: dict[str, object]) -> str:
    import base64
    import json

    raw = json.dumps(payload, separators=(",", ":"), sort_keys=True).encode()
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def test_terminal_wire_codec() -> None:
    frame = terminal_wire.encode_output("e1", 0, b"hello")
    chunk = terminal_wire.decode_output(frame)
    assert (chunk.epoch, chunk.offset, chunk.data) == ("e1", 0, b"hello")
    frame = terminal_wire.encode_text("lease-1", 3, 1, b"\x03")
    parsed = terminal_wire.decode_input(frame)
    assert (parsed.lease_id, parsed.generation, parsed.input_seq, parsed.kind) == (
        "lease-1",
        3,
        1,
        "text",
    )
    assert parsed.data == b"\x03"
    key = terminal_wire.encode_key("lease-1", 3, 2, "ctrl+c")
    assert terminal_wire.decode_input(key).kind == "key"
    resize = terminal_wire.encode_resize("lease-1", 3, 3, 100, 30)
    sized = terminal_wire.decode_input(resize)
    assert (sized.kind, sized.cols, sized.rows) == ("resize", 100, 30)
    with pytest.raises(ValueError):
        terminal_wire.encode_text("l", 1, 1, b"x" * (terminal_wire.MAX_INPUT_CHUNK + 1))
    with pytest.raises(ValueError):
        terminal_wire.encode_key("l", 1, 1, "home")  # outside upstream grammar
    with pytest.raises(ValueError):
        terminal_wire.encode_resize("l", 1, 1, 0, 30)
    with pytest.raises(ValueError):
        terminal_wire.decode_input(b"\x01" + b"0" * 20)  # v1 rejected
    with pytest.raises(ValueError):
        terminal_wire.decode_output(b"short")
