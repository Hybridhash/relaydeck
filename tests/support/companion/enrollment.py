"""Operator enrollment request bodies."""

from __future__ import annotations

import base64
from typing import Any

from relaydeck.connector.pubkey import sign_proof


def _redeem_body(
    seed: bytes, public: str, invitation_id: str, code: str, host_id: str
) -> dict[str, Any]:
    proof = sign_proof(seed, invitation_id, host_id)
    return {
        "code": code,
        "host_id": host_id,
        "pubkey": public,
        "proof": base64.urlsafe_b64encode(proof).decode().rstrip("="),
    }
