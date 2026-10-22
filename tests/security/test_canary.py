"""Synthetic secrets through every emitter.

Victim credentials (real session/token/ticket/host-token bytes minted
by the stores under test), digests, passwords, drafts, clipboard
content, and push tokens flow through auth, terminal, journal, push,
and error paths. ZERO canary bytes may appear in error responses,
close reasons, exception messages, logs, or persisted non-secret
stores. Intended secret holders (session/ticket/host stores,
journal intent bodies, the push token column, clipboard memory) are
asserted separately so the forbidden-set test cannot pass vacuously.
"""

from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path
from typing import Any

import pytest
from starlette.websockets import WebSocketDisconnect

from relaydeck.gateway.revocation import revoke_account_and_close
from tests.security.helpers import (
    ALL_CAPS,
    NOW,
    attach,
    auth,
    command_body,
    lease,
    make_client,
    mint_token,
    ticket,
)
from tests.support.fakes import FakeClipboard

CANARY_PASSWORD = "canary-pw-7f3a9c2e-hunter2"
CANARY_DRAFT = "canary-draft-4b8d1e6f-type-this"
CANARY_CLIP = "canary-clipboard-9c1e5a77-copyme"
CANARY_PUSH = "canary-push-token-2d6b4f81-fcm"
CANARY_EVENT = "canary-event-5e9c3a17-evil"


def test_error_responses_carry_no_victim_secrets() -> None:
    client, state, backend = make_client()
    access, refresh = state.sessions.create("victim", ALL_CAPS, frozenset({"home-mac"}), NOW)
    att = attach(client, access)
    ls = lease(client, access, att["attachment_id"])
    tick = ticket(client, access, ls["lease_id"])
    host_token = state.hosts.enroll("home-mac", "Home", "victim")
    victim_secrets = [access, refresh, tick, host_token]
    seen: list[tuple[str, str]] = []

    def grab(name: str, body: Any) -> None:
        seen.append((name, json.dumps(body, sort_keys=True, default=str)))

    # Attacks that produce errors while victim secrets are live.
    grab("anon-me", client.get("/v1/me").json())
    grab("bogus-me", client.get("/v1/me", headers=auth("bogus")).json())
    grab("cmd-anon", client.post("/v1/hosts/home-mac/commands", json=command_body()).json())
    grab(
        "cmd-conflict",
        client.post(
            "/v1/hosts/home-mac/commands", headers=auth(access), json=command_body()
        ).json(),
    )
    other_body = command_body()
    other_body["payload"] = {"text": "different"}
    grab(
        "cmd-conflict2",
        client.post("/v1/hosts/home-mac/commands", headers=auth(access), json=other_body).json(),
    )
    grab(
        "ticket-foreign",
        client.post(
            "/v1/terminal-tickets",
            headers=auth(mint_token(state, "attacker")),
            json={"lease_id": ls["lease_id"]},
        ).json(),
    )
    grab(
        "renew-foreign",
        client.post(
            f"/v1/terminal-leases/{ls['lease_id']}/renew",
            headers=auth(mint_token(state, "attacker")),
            json={},
        ).json(),
    )
    grab(
        "agents-bad-cursor",
        client.get(
            "/v1/hosts/home-mac/agents",
            headers=auth(access),
            params={"page_token": "forged-cursor"},
        ).json(),
    )
    grab(
        "deep-link-forged",
        client.post(
            "/v1/deep-links/resolve",
            headers=auth(access),
            json={"token": "rdl_forged0000000000000000000001"},
        ).json(),
    )
    grab(
        "device-rotate-unknown",
        client.post("/v1/devices/nope/rotate", headers=auth(access), json={"token": "x"}).json(),
    )
    grab(
        "redeem-unknown",
        client.post(
            "/v1/enrollments/enr_nope/redeem",
            json={
                "code": "AAAAAAAAAAAA",
                "host_id": "h",
                "pubkey": "ssh-ed25519 AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA=",
                "proof": "A" * 86,
            },
        ).json(),
    )
    grab(
        "cmd-status",
        client.get("/v1/hosts/home-mac/commands/cmd-sec-001", headers=auth(access)).json(),
    )

    assert len(seen) >= 12
    for name, blob in seen:
        for secret in victim_secrets:
            assert secret not in blob, f"victim secret in {name}"
        for marker in (CANARY_PASSWORD, CANARY_DRAFT, CANARY_CLIP, CANARY_PUSH):
            assert marker not in blob, f"canary marker in {name}"
    # Sanity: the victim session genuinely holds its secrets.
    assert state.sessions.lookup(access, NOW) is not None
    assert backend.open_lease(ls["lease_id"]) is not None


def test_ws_close_reasons_are_fixed_codes() -> None:
    """Every close reason emitted by the gateway is a literal from the
    fixed code set or an enum-bound value (error code, ring status,
    reset detail) -- never requester or victim data."""
    root = Path(__file__).resolve().parent.parent.parent / "src" / "relaydeck"
    # The WebSocket handlers live in the sockets router; app.py keeps the rest.
    sources = [
        (root / "gateway" / "app.py").read_text(),
        (root / "gateway" / "api" / "sockets.py").read_text(),
    ]
    app_source = "\n".join(sources)
    import ast
    import re

    reasons = [
        ast.unparse(keyword.value)
        for source in sources
        for node in ast.walk(ast.parse(source))
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "close"
        for keyword in node.keywords
        if keyword.arg == "reason"
    ]
    assert reasons, "expected close reasons in the gateway app and sockets router"
    allowed_vars = {"exc.code", "stream.status", "push_detail"}
    for reason in reasons:
        reason = reason.strip()
        if reason.startswith(('"', "'")):
            continue  # literal code
        assert reason in allowed_vars, f"non-enum close reason: {reason}"
    # Registry callers pass literals only.
    call_reasons = re.findall(r"close_\w+\([^)]*\"([^\"]+)\"\)", app_source)
    assert call_reasons
    # Inspect actual socket.close calls, not diagnostic reason= arguments.
    sockets_source = (root / "gateway" / "sockets.py").read_text()
    socket_reasons = [
        ast.unparse(keyword.value)
        for node in ast.walk(ast.parse(sockets_source))
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "close"
        for keyword in node.keywords
        if keyword.arg == "reason"
    ]
    assert socket_reasons and set(socket_reasons) == {"reason"}
    # ...and the web guard passes a literal at its one registry call.
    serve_source = (root / "client" / "serve.py").read_text()
    assert "reason=" not in serve_source
    serve_calls = re.findall(
        r"close_(?:account|device|lease|host)\(([^)]*)\)", serve_source, re.DOTALL
    )
    assert len(serve_calls) == 1
    assert re.search(r',\s*"[a-z_]+"\s*$', serve_calls[0]), serve_calls


def test_registry_close_logs_and_reason_are_content_free(caplog: pytest.LogCaptureFixture) -> None:
    from relaydeck.gateway.sockets import SocketRegistry
    from relaydeck.lease_diagnostics import LeaseTrace

    closes: list[tuple[int, str]] = []

    class Socket:
        async def close(self, *, code: int, reason: str) -> None:
            closes.append((code, reason))

    async def scenario() -> None:
        registry = SocketRegistry()
        socket = Socket()
        registry.register(
            CANARY_DRAFT,
            CANARY_PASSWORD,
            socket,
            CANARY_PUSH,
            diagnostics=LeaseTrace.for_session(
                socket, CANARY_CLIP, CANARY_EVENT, CANARY_PASSWORD, "control", 7
            ),
        )
        assert await registry.close_device(CANARY_DRAFT, CANARY_PUSH, "unauthenticated") == 1
        assert len(registry) == 0

    with caplog.at_level(logging.INFO, logger="relaydeck.lease_lifecycle"):
        asyncio.run(scenario())
    assert closes == [(1008, "unauthenticated")]
    records = [r for r in caplog.records if r.name == "relaydeck.lease_lifecycle"]
    assert records and "reason=unauthenticated" in records[0].getMessage()
    for record in records:
        raw = repr((record.msg, record.args, record.exc_info))
        for marker in (CANARY_PASSWORD, CANARY_DRAFT, CANARY_CLIP, CANARY_PUSH, CANARY_EVENT):
            assert marker not in raw
        assert record.exc_info is None


def test_close_reason_values_observed_are_safe() -> None:
    client, state, _ = make_client()
    token = mint_token(state)
    att = attach(client, token)
    ls = lease(client, token, att["attachment_id"])
    tick = ticket(client, token, ls["lease_id"])
    with pytest.raises(WebSocketDisconnect) as exc_info:
        with client.websocket_connect(f"/v1/terminal/{ls['lease_id']}?ticket=rdt_bogus"):
            pass
    assert exc_info.value.code == 1008
    assert getattr(exc_info.value, "reason", None) in (None, "unauthenticated")
    with pytest.raises(WebSocketDisconnect) as exc_info:
        with client.websocket_connect("/v1/connectors?token=bogus"):
            pass
    assert exc_info.value.code == 1008
    _ = tick  # victim ticket minted but never exposed on close


def test_exceptions_carry_no_canaries() -> None:
    from relaydeck.gateway.backend import BackendError, FakeBackend
    from relaydeck.gateway.leases import LeaseError, LeaseStore
    from relaydeck.gateway.push import PushDenied, PushStore
    from relaydeck.journal import JournalConflict

    messages: list[str] = []

    def grab(exc: BaseException) -> None:
        messages.append(f"{type(exc).__name__}: {exc} {getattr(exc, 'code', '')}")

    backend = FakeBackend(now_ms=lambda: NOW)
    try:
        backend.snapshot(CANARY_EVENT)
    except BackendError as exc:
        grab(exc)  # echoes the requester host id only
    leases = LeaseStore()
    try:
        leases.renew("lease-nope", "acct-x", "dev-x", NOW)
    except LeaseError as exc:
        grab(exc)
    store = PushStore()
    try:
        try:
            store.register_device("a", "", CANARY_PUSH, "android", NOW)
        except PushDenied as exc:
            grab(exc)
        try:
            store.notify(
                account="a",
                account_hosts=frozenset({"h"}),
                host_id=" elsewhere ",
                session_id="s",
                event_id=CANARY_EVENT,
                category="bogus",
                now=NOW,
            )
        except PushDenied as exc:
            grab(exc)
    finally:
        store.close()
    grab(JournalConflict("cmd-canary-1"))
    assert len(messages) >= 4
    for message in messages:
        for marker in (CANARY_PASSWORD, CANARY_DRAFT, CANARY_CLIP, CANARY_PUSH):
            assert marker not in message
    # The requester-controlled id echo is the documented exception: the
    # caller supplied it, so its presence proves nothing about leaks.
    assert any(CANARY_EVENT in message for message in messages)


def test_logs_carry_no_canaries(caplog: Any, capsys: Any) -> None:
    """No logging/printf emitter in the request path: drive auth,
    terminal, journal, and push traffic with live canaries and assert
    the captured logs and stdio stay canary-free."""
    from relaydeck.client.features.composer.domain import ComposerStore

    submits: list[dict[str, Any]] = []

    async def submit_command(body: dict[str, Any]) -> dict[str, Any]:
        submits.append(body)
        return {"status": "accepted"}

    async def prompt_text(target: str, text: str) -> dict[str, Any]:
        return {"type": "agent_prompted"}

    async def scenario() -> None:
        clipboard = FakeClipboard()
        await clipboard.set_text(CANARY_CLIP)
        composer = ComposerStore(
            target="a",
            submit_command=submit_command,
            prompt_text=prompt_text,
            host_id="home-mac",
            host_epoch="e",
            agent_id="a",
        )
        composer.replace_draft(CANARY_DRAFT)
        composer.agent_status_did_change("blocked", CANARY_PASSWORD)
        assert composer.draft == CANARY_DRAFT

    with caplog.at_level(logging.DEBUG):
        client, state, backend = make_client()
        access, _ = state.sessions.create("victim", ALL_CAPS, frozenset({"home-mac"}), NOW)
        state.push.register_device("victim", "install-v", CANARY_PUSH, "android", NOW)
        client.post(
            "/v1/hosts/home-mac/commands",
            headers=auth(access),
            json=command_body("cmd-sec-c1", text=CANARY_DRAFT),
        )
        att = attach(client, access)
        lease(client, access, att["attachment_id"])
        asyncio.run(revoke_account_and_close(state, "victim"))
        import asyncio as _aio

        _aio.run(scenario())
        assert backend.command_status("cmd-sec-c1") is not None

    blob = caplog.text + capsys.readouterr().out + capsys.readouterr().err
    for marker in (CANARY_PASSWORD, CANARY_DRAFT, CANARY_CLIP, CANARY_PUSH, CANARY_EVENT):
        assert marker not in blob
    assert access not in blob


def test_persisted_non_secret_stores_carry_no_credentials(tmp_path: Any) -> None:
    """Credential canaries appear ONLY in their intended holders
    (session store, push token column); never in outbox/refs/links,
    journal routing columns, or error-adjacent fields."""
    import sqlite3

    from relaydeck.journal import CommandJournal

    client, state, backend = make_client()
    access, _ = state.sessions.create("victim", ALL_CAPS, frozenset({"home-mac"}), NOW)
    tick = ticket(
        client, access, lease(client, access, attach(client, access)["attachment_id"])["lease_id"]
    )
    now = state.now_ms()
    state.push.register_device("victim", "install-v", CANARY_PUSH, "android", now)
    minted = state.push.notify(
        account="victim",
        account_hosts=frozenset({"home-mac"}),
        host_id="home-mac",
        session_id="sess-v",
        event_id=CANARY_EVENT,
        category="attention",
        now=now,
    )
    assert minted
    client.post(
        "/v1/hosts/home-mac/commands",
        headers=auth(access),
        json=command_body("cmd-sec-c2", text=CANARY_DRAFT),
    )

    push_db = state.push._db  # in-memory test store; same schema as file
    assert isinstance(push_db, sqlite3.Connection)
    non_secret_rows: list[str] = []
    for table in ("push_outbox", "push_refs", "push_links"):
        for row in push_db.execute(f"SELECT * FROM {table}").fetchall():
            non_secret_rows.append(" ".join("" if v is None else str(v) for v in tuple(row)))
    token_cells = [row[0] for row in push_db.execute("SELECT token FROM push_devices").fetchall()]
    assert token_cells == [CANARY_PUSH]  # intended holder, and only there
    for blob in non_secret_rows:
        for marker in (access, tick, CANARY_PUSH, CANARY_PASSWORD, CANARY_CLIP):
            assert marker not in blob, f"credential in non-secret push row: {blob[:120]}"

    journal = CommandJournal(tmp_path / "canary.db")
    try:
        journal.submit(
            command_id="cmd-sec-c3",
            principal="victim",
            host_id="home-mac",
            kind="agent.prompt",
            target={"agent_id": "a"},
            payload={"text": CANARY_DRAFT},
            now=NOW,
        )
        record = journal.record("cmd-sec-c3")
        assert record is not None
        assert CANARY_DRAFT in str(record["payload"])  # intended intent body
        routing = " ".join(
            str(record[key])
            for key in ("command_id", "principal", "host_id", "kind", "status", "digest")
        )
        for marker in (access, tick, CANARY_PUSH, CANARY_PASSWORD, CANARY_CLIP):
            assert marker not in routing
    finally:
        journal.close()
    _ = backend
