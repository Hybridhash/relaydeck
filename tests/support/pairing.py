"""Synthetic host/store; no provider, listener or live Herdr."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from relaydeck.companion.private import private_dir
from relaydeck.connector.pubkey import fingerprint_of_raw, generate, openssh_to_raw
from relaydeck.gateway.app import GatewayState
from relaydeck.gateway.hosts import HostStore
from relaydeck.gateway.identity import pkce_challenge
from relaydeck.gateway.pairing import PairingStore
from tests.security.helpers import make_state

ORIGIN = "https://test-mac.synthetic-tailnet.ts.net"
VERIFIER = "synthetic-device-verifier-" + "x" * 43


def foundation(path: Path) -> tuple[GatewayState, list[int], dict[str, Any]]:
    private_dir(path)
    clock = [1_000_000]
    state, _ = make_state(hosts=("home-mac", "lab-mac"))
    state.now_ms = lambda: clock[0]
    state.public_origin = ORIGIN
    state.hosts = HostStore(path=path / "hosts.json")
    for host in ("home-mac", "lab-mac"):
        _, public = generate()
        state.hosts.enroll(host, host, "owner", public, fingerprint_of_raw(openssh_to_raw(public)))
    state.pairing = PairingStore(path / "pairing.json")
    state.pairing.start(ORIGIN)
    body = {
        "origin": ORIGIN,
        "host_id": "home-mac",
        "code_challenge": pkce_challenge(VERIFIER),
        "code_challenge_method": "S256",
        "device_label": "Synthetic phone",
    }
    return state, clock, body


def proof(**overrides: Any) -> dict[str, str]:
    return {"origin": ORIGIN, "host_id": "home-mac", "verifier": VERIFIER, **overrides}
