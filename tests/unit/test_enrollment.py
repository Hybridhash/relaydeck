"""Host store, connector keys/creds, enrollment claim, CLIs."""

from __future__ import annotations

import asyncio
import base64
import json
import os
import stat
from typing import Any

import pytest

from relaydeck.connector import cli as connector_cli
from relaydeck.connector.enrollment import (
    ConnectorCreds,
    ConnectorKeys,
    EnrollmentError,
    claim_enrollment,
    generate_keys,
    load_creds,
    load_keys,
    parse_invitation,
    save_creds,
    save_keys,
    wipe_creds,
)
from relaydeck.connector.pubkey import generate
from relaydeck.gateway import cli as gateway_cli
from relaydeck.gateway.hosts import HostStore
from relaydeck.gateway.identity import EnrollmentStore


def _keys() -> ConnectorKeys:
    seed, public = generate()
    return ConnectorKeys(base64.b64encode(seed).decode(), public)


def test_host_lifecycle() -> None:
    store = HostStore()
    first = store.enroll("h1", "Home", "acct-1", "ssh-ed25519 AAAA", "SHA256:x")
    assert first.startswith("rdh_")
    record = store.lookup(first)
    assert record is not None and record.host_id == "h1"
    assert (record.account, record.pubkey, record.fingerprint) == (
        "acct-1",
        "ssh-ed25519 AAAA",
        "SHA256:x",
    )
    assert store.record_for_host("h1") == record
    assert [h.host_id for h in store.hosts()] == ["h1"]
    second = store.enroll("h1")
    assert store.lookup(first) is None  # rotation kills the old token
    assert store.lookup(second) is not None
    assert store.revoke("h1") is True
    assert store.lookup(second) is None
    assert store.record_for_host("h1") is None
    assert store.revoke("h1") is False
    assert store.lookup("") is None


def test_creds_round_trip_and_perms(tmp_path) -> None:  # type: ignore[no-untyped-def]
    path = str(tmp_path / "connector.json")
    assert load_creds(path) is None
    saved = save_creds(ConnectorCreds("https://gw", "h1", "rdh_x"), path)
    mode = stat.S_IMODE(os.stat(saved).st_mode)
    assert mode == 0o600
    creds = load_creds(path)
    assert creds == ConnectorCreds("https://gw", "h1", "rdh_x")
    assert wipe_creds(path) is True
    assert wipe_creds(path) is False


def test_creds_corrupt(tmp_path) -> None:  # type: ignore[no-untyped-def]
    path = tmp_path / "connector.json"
    path.write_text("not json")
    with pytest.raises(EnrollmentError):
        load_creds(str(path))
    path.write_text(json.dumps({"host_id": "h"}))
    with pytest.raises(EnrollmentError):
        load_creds(str(path))


def test_keys_round_trip_and_perms(tmp_path) -> None:  # type: ignore[no-untyped-def]
    path = str(tmp_path / "connector_key.json")
    assert load_keys(path) is None
    keys = generate_keys()
    assert keys.fingerprint.startswith("SHA256:")
    saved = save_keys(keys, path)
    assert stat.S_IMODE(os.stat(saved).st_mode) == 0o600
    assert load_keys(path) == keys


def test_keys_corrupt(tmp_path) -> None:  # type: ignore[no-untyped-def]
    path = tmp_path / "connector_key.json"
    path.write_text("not json")
    with pytest.raises(EnrollmentError):
        load_keys(str(path))
    path.write_text(json.dumps({"seed_b64": "AAAA"}))
    with pytest.raises(EnrollmentError):
        load_keys(str(path))
    keys = generate_keys()
    bad = ConnectorKeys(keys.seed_b64, "ssh-rsa AAAA")
    bad_path = str(tmp_path / "bad.json")
    save_keys(bad, bad_path)
    with pytest.raises(EnrollmentError):
        load_keys(bad_path)


def test_parse_invitation() -> None:
    from relaydeck.connector.pubkey import build_qr_payload

    qr = build_qr_payload("https://gw.example", "enr_1", "ABCD1234EFGH", "2026-10-03T07:00:00Z")
    assert parse_invitation(qr) == (
        "https://gw.example",
        "enr_1",
        "ABCD1234EFGH",
        "2026-10-03T07:00:00Z",
    )
    with pytest.raises(EnrollmentError):
        parse_invitation("HERDR-PAIR:1:AAAA")


def test_claim_enrollment() -> None:
    async def scenario() -> None:
        keys = _keys()

        async def ok(url: str, body: dict[str, Any]) -> tuple[int, Any]:
            assert url == "https://gw/v1/enrollments/enr_1/redeem"
            assert body["code"] == "ABCD1234EFGH"
            assert body["host_id"] == "h"
            assert body["pubkey"] == keys.public_openssh
            assert len(body["proof"]) == 86
            return 200, {"host_token": "rdh_new", "fingerprint": keys.fingerprint}

        token, echoed = await claim_enrollment(ok, "https://gw", "h", "enr_1", "ABCD1234EFGH", keys)
        assert (token, echoed) == ("rdh_new", keys.fingerprint)

        async def denied(url: str, body: dict[str, Any]) -> tuple[int, Any]:
            return 404, {"message": "no such enrollment"}

        with pytest.raises(EnrollmentError, match="no such enrollment"):
            await claim_enrollment(denied, "https://gw", "h", "enr_1", "ABCD1234EFGH", keys)

        async def swapped(url: str, body: dict[str, Any]) -> tuple[int, Any]:
            return 200, {"host_token": "rdh_new", "fingerprint": "SHA256:" + "x" * 43}

        with pytest.raises(EnrollmentError, match="different fingerprint"):
            await claim_enrollment(swapped, "https://gw", "h", "enr_1", "ABCD1234EFGH", keys)

        with pytest.raises(EnrollmentError):
            await claim_enrollment(ok, "https://gw", "", "enr_1", "ABCD1234EFGH", keys)
        with pytest.raises(EnrollmentError):
            await claim_enrollment(ok, "https://gw", "h", "", "ABCD1234EFGH", keys)

    asyncio.run(scenario())


def test_enrollment_store_attempt_claim_expiry_burn() -> None:
    store = EnrollmentStore()
    assert store.attempt("nope", "whatever", 1_000) is None
    invitation = store.create("acct-1", "Home", "ssh-ed25519 AAAA", "SHA256:x", 1_000)
    assert invitation.id.startswith("enr_") and len(invitation.code) == 12
    # Wrong codes burn attempts; the 5th guess burns the invitation.
    for _ in range(5):
        assert store.attempt(invitation.id, "WRONGCODE123", 1_000) is None
    assert store.attempt(invitation.id, invitation.code, 1_000) is None
    assert store.claim(invitation.id) is True  # claim still flips the flag

    fresh = store.create("acct-1", "Home", "ssh-ed25519 AAAA", "SHA256:x", 1_000)
    grouped = f"{fresh.code[:4]}-{fresh.code[4:8]}-{fresh.code[8:]}".lower()
    assert store.attempt(fresh.id, grouped, 1_000) == fresh
    assert store.claim(fresh.id) is True
    assert store.claim(fresh.id) is False
    assert store.attempt(fresh.id, fresh.code, 1_000) is None  # claimed

    expired = store.create("acct-1", "Home", "ssh-ed25519 AAAA", "SHA256:x", 0, ttl_ms=100)
    assert store.attempt(expired.id, expired.code, 1_000) is None


def test_revocation_still_closed() -> None:
    # HostStore.revoke stays the token-kill primitive; socket/lease
    # severing is covered by revoke_host_and_close tests.
    assert HostStore().revoke("ghost") is False


def test_connector_cli_parsing() -> None:
    args = connector_cli.build_parser().parse_args(
        ["enroll", "--gateway", "https://gw", "--host", "h", "--id", "enr_1", "--code", "C"]
    )
    assert (args.gateway, args.host, args.id, args.code) == ("https://gw", "h", "enr_1", "C")
    args = connector_cli.build_parser().parse_args(["enroll", "--host", "h", "--qr", "Q"])
    assert args.qr == "Q"
    assert connector_cli.build_parser().parse_args(["keygen"]).command == "keygen"
    assert connector_cli.build_parser().parse_args(["fingerprint"]).command == "fingerprint"
    assert connector_cli.build_parser().parse_args(["status"]).command == "status"
    assert connector_cli.build_parser().parse_args(["revoke"]).command == "revoke"
    assert connector_cli.main(["run"]) == 2


def test_gateway_cli_parsing(capsys) -> None:  # type: ignore[no-untyped-def]
    keys = _keys()
    args = gateway_cli.build_parser().parse_args(
        ["issue-enrollment", "--host-label", "H", "--pubkey", keys.public_openssh]
    )
    assert (args.host_label, args.account) == ("H", "owner")
    assert gateway_cli.main(["list-hosts"]) == 2
    assert gateway_cli.main(["revoke-host", "--host", "h"]) == 2
    assert (
        gateway_cli.main(
            [
                "issue-enrollment",
                "--host-label",
                "H",
                "--pubkey",
                keys.public_openssh,
                "--origin",
                "https://gw.example",
            ]
        )
        == 0
    )
    out = capsys.readouterr().out
    assert "fingerprint SHA256:" in out and "qr RELAYDECK-ENROLL:1:" in out
    assert gateway_cli.main(["issue-enrollment", "--host-label", "H", "--pubkey", "junk"]) == 1
