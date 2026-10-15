"""Enrollment replay, attempt burn, and key binding.

The invitation (id + code) is redeemable exactly once, code guesses
burn a 5-attempt budget, and redemption is bound to the enrolled
connector public key, the invitation id, and the chosen host id. An
attacker with a copied code but without the private seed gains
nothing; cross-account redemption enrolls under the invitation's
account, never the attacker's.
"""

from __future__ import annotations

import base64
from typing import Any

from relaydeck.connector.enrollment import ConnectorKeys
from relaydeck.connector.pubkey import generate, sign_proof
from tests.security.helpers import auth, make_client, mint_token


def _keys() -> ConnectorKeys:
    seed, public = generate()
    return ConnectorKeys(base64.b64encode(seed).decode(), public)


def _redeem(keys: ConnectorKeys, invitation_id: str, code: str, host_id: str) -> dict[str, Any]:
    seed = base64.b64decode(keys.seed_b64)
    proof = sign_proof(seed, invitation_id, host_id)
    return {
        "code": code,
        "host_id": host_id,
        "pubkey": keys.public_openssh,
        "proof": base64.urlsafe_b64encode(proof).decode().rstrip("="),
    }


def _invite(client: Any, token: str, keys: ConnectorKeys) -> dict[str, Any]:
    r = client.post(
        "/v1/enrollments",
        headers=auth(token),
        json={"host_label": "Home Mac", "pubkey": keys.public_openssh},
    )
    assert r.status_code == 201, r.text
    invitation: dict[str, Any] = r.json()
    return invitation


def test_used_code_reuse_fails() -> None:
    client, state, _ = make_client()
    token = mint_token(state, "acct-a")
    keys = _keys()
    invitation = _invite(client, token, keys)
    url = f"/v1/enrollments/{invitation['id']}/redeem"
    first = client.post(url, json=_redeem(keys, invitation["id"], invitation["code"], "h1"))
    assert first.status_code == 200
    replay = client.post(url, json=_redeem(keys, invitation["id"], invitation["code"], "h1"))
    assert replay.json()["code"] == "not_found"
    # A second host id does not resurrect the consumed invitation.
    replay2 = client.post(url, json=_redeem(keys, invitation["id"], invitation["code"], "h2"))
    assert replay2.json()["code"] == "not_found"


def test_code_guessing_burns_attempts() -> None:
    client, state, _ = make_client()
    token = mint_token(state, "acct-a")
    keys = _keys()
    invitation = _invite(client, token, keys)
    url = f"/v1/enrollments/{invitation['id']}/redeem"
    for _ in range(5):
        r = client.post(url, json=_redeem(keys, invitation["id"], "ZZZZZZZZZZZZ", "home-mac"))
        assert r.json()["code"] == "not_found"
    # The budget is spent: even the true code now fails.
    burned = client.post(url, json=_redeem(keys, invitation["id"], invitation["code"], "home-mac"))
    assert burned.json()["code"] == "not_found"


def test_wrong_key_cannot_redeem() -> None:
    client, state, _ = make_client()
    token = mint_token(state, "acct-a")
    keys = _keys()
    attacker = _keys()
    invitation = _invite(client, token, keys)
    url = f"/v1/enrollments/{invitation['id']}/redeem"
    # Attacker's own key: invitation/key mismatch.
    r = client.post(url, json=_redeem(attacker, invitation["id"], invitation["code"], "home-mac"))
    assert r.json()["code"] == "forbidden"
    # Victim pubkey line + attacker proof: signature mismatch.
    body = _redeem(attacker, invitation["id"], invitation["code"], "home-mac")
    body["pubkey"] = keys.public_openssh
    r = client.post(url, json=body)
    assert r.json()["code"] == "forbidden"
    # The legitimate connector still redeems afterwards.
    ok = client.post(url, json=_redeem(keys, invitation["id"], invitation["code"], "home-mac"))
    assert ok.status_code == 200


def test_proof_bound_to_invitation_and_host() -> None:
    client, state, _ = make_client()
    token = mint_token(state, "acct-a")
    keys = _keys()
    first = _invite(client, token, keys)
    second = _invite(client, token, keys)
    # Proof minted for invitation #1 is refused on invitation #2.
    body = _redeem(keys, first["id"], second["code"], "home-mac")
    r = client.post(f"/v1/enrollments/{second['id']}/redeem", json=body)
    assert r.json()["code"] == "forbidden"
    # Proof minted for host h1 is refused when redeeming host h2.
    body = _redeem(keys, second["id"], second["code"], "other-host")
    body["host_id"] = "home-mac"
    r = client.post(f"/v1/enrollments/{second['id']}/redeem", json=body)
    assert r.json()["code"] == "forbidden"


def test_cross_account_redeem_binds_invitation_account() -> None:
    """Redeeming binds the host record to the invitation owner's
    account (the redeem call itself is unauthenticated by design: the
    code + proof-of-possession ARE the credential). An attacker who
    somehow holds a live code still enrolls under the victim account
    with the victim-bound key -- never under their own account, and
    never with their own key (proven above)."""
    client, state, _ = make_client()
    token_a = mint_token(state, "acct-a")
    mint_token(state, "acct-b")
    keys = _keys()
    invitation = _invite(client, token_a, keys)
    r = client.post(
        f"/v1/enrollments/{invitation['id']}/redeem",
        json=_redeem(keys, invitation["id"], invitation["code"], "shared-host"),
    )
    assert r.status_code == 200
    record = state.hosts.record_for_host("shared-host")
    assert record is not None
    assert record.account == "acct-a"
    assert record.fingerprint == keys.fingerprint


def test_expired_invitation_fails() -> None:
    client, state, _ = make_client(now=1_000_000)
    token = mint_token(state, "acct-a")
    keys = _keys()
    invitation = _invite(client, token, keys)
    state.now_ms = lambda: 1_000_000 + 11 * 60 * 1000  # past the 10-minute TTL
    r = client.post(
        f"/v1/enrollments/{invitation['id']}/redeem",
        json=_redeem(keys, invitation["id"], invitation["code"], "home-mac"),
    )
    assert r.json()["code"] == "not_found"


def test_unknown_invitation_indistinguishable() -> None:
    client, _, _ = make_client()
    keys = _keys()
    r = client.post(
        "/v1/enrollments/enr_no-such-id/redeem",
        json=_redeem(keys, "enr_no-such-id", "AAAAAAAAAAAA", "home-mac"),
    )
    assert r.json()["code"] == "not_found"
    assert "details" not in r.json()


def test_cli_issued_invitation_redeems_over_http(tmp_path: Any) -> None:
    """The operator CLI mints into the shared file and the
    serving gateway redeems from it (the documented ceremony)."""
    import os
    import stat

    from relaydeck.connector.pubkey import (
        fingerprint_of_raw,
        openssh_to_raw,
        raw_to_openssh,
    )
    from relaydeck.gateway.identity import EnrollmentStore

    store = str(tmp_path / "enrollments.json")
    client, state, _ = make_client()
    state.enrollments = EnrollmentStore(path=store)
    keys = _keys()
    raw = openssh_to_raw(keys.public_openssh)
    invitation = EnrollmentStore(path=store).create(
        "acct-a", "Home Mac", raw_to_openssh(raw), fingerprint_of_raw(raw), 1_000_000
    )
    assert stat.S_IMODE(os.stat(store).st_mode) == 0o600
    r = client.post(
        f"/v1/enrollments/{invitation.id}/redeem",
        json=_redeem(keys, invitation.id, invitation.code, "home-mac"),
    )
    assert r.status_code == 200, r.text
    record = state.hosts.record_for_host("home-mac")
    assert record is not None and record.account == "acct-a"


def test_http_issued_invitation_visible_to_cli_side(tmp_path: Any) -> None:
    """Write-through runs both directions: HTTP-created invitations land
    in the shared file where a second (CLI-side) reader sees them."""
    from relaydeck.gateway.identity import EnrollmentStore

    store = str(tmp_path / "enrollments.json")
    client, state, _ = make_client()
    state.enrollments = EnrollmentStore(path=store)
    token = mint_token(state, "acct-a")
    keys = _keys()
    invitation = _invite(client, token, keys)
    found = EnrollmentStore(path=store).attempt(invitation["id"], invitation["code"], 1_000_000)
    assert found is not None
    assert found.account == "acct-a"
