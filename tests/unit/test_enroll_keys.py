"""Enrollment keys, proofs and QR bounds."""

from __future__ import annotations

import base64
import shutil
import subprocess
from pathlib import Path

import pytest

from relaydeck.connector import pubkey


def test_key_roundtrip_and_proof() -> None:
    seed, line = pubkey.generate()
    assert len(seed) == 32
    assert line.startswith("ssh-ed25519 ")
    raw = pubkey.openssh_to_raw(line)
    assert pubkey.raw_to_openssh(raw) == line
    assert pubkey.fingerprint_of_openssh(line) == pubkey.fingerprint_of_raw(raw)
    assert pubkey.fingerprint_of_raw(raw).startswith("SHA256:")
    sig = pubkey.sign_proof(seed, "enr_1", "home-mac")
    assert len(sig) == 64
    assert pubkey.verify_proof(line, "enr_1", "home-mac", sig) is True
    assert pubkey.verify_proof(line, "enr_1", "other-mac", sig) is False
    assert pubkey.verify_proof(line, "enr_2", "home-mac", sig) is False
    assert pubkey.verify_proof(line, "enr_1", "home-mac", b"x" * 64) is False
    _, other = pubkey.generate()
    assert pubkey.verify_proof(other, "enr_1", "home-mac", sig) is False


def test_openssh_rejects_wrong_types() -> None:
    with pytest.raises(ValueError):
        pubkey.openssh_to_raw("ssh-rsa AAAAqW90")
    with pytest.raises(ValueError):
        pubkey.openssh_to_raw("ssh-ed25519 !!!not-base64!!!")
    with pytest.raises(ValueError):
        pubkey.openssh_to_raw("ssh-ed25519 " + base64.b64encode(b"short").decode())
    # Trailing comments are tolerated (stripped by split).
    _, line = pubkey.generate()
    assert pubkey.openssh_to_raw(line + " owner@mac") == pubkey.openssh_to_raw(line)


def test_fingerprint_matches_ssh_keygen(tmp_path: Path) -> None:
    tool = shutil.which("ssh-keygen")
    if tool is None:
        pytest.skip("ssh-keygen unavailable")
    _, line = pubkey.generate()
    key_file = tmp_path / "id_relaydeck.pub"
    key_file.write_text(line + "\n")
    out = subprocess.run(
        [tool, "-lf", str(key_file)], capture_output=True, text=True, check=True, timeout=30
    ).stdout.strip()
    # "256 SHA256:... owner@mac (ED25519)"
    assert out.split()[1] == pubkey.fingerprint_of_openssh(line)


def test_qr_roundtrip_bounds_and_shape() -> None:
    payload = pubkey.build_qr_payload(
        "https://gw.example", "enr_abc123", "ABCD-EFGH-JKLM", "2026-10-03T07:00:00Z"
    )
    assert payload.startswith("RELAYDECK-ENROLL:1:")
    assert len(payload) <= pubkey.MAX_QR_LEN
    doc = pubkey.parse_qr_payload(payload)
    assert doc == {
        "gw": "https://gw.example",
        "id": "enr_abc123",
        "code": "ABCD-EFGH-JKLM",
        "exp": "2026-10-03T07:00:00Z",
        "v": "1",
    }
    assert (
        pubkey.parse_qr_payload(pubkey.build_qr_payload("http://127.0.0.1:8000", "e", "C", "t"))[
            "gw"
        ]
        == "http://127.0.0.1:8000"
    )
    with pytest.raises(ValueError):
        pubkey.parse_qr_payload("HERDR-PAIR:1:AAAA")
    with pytest.raises(ValueError):
        pubkey.parse_qr_payload(payload.replace(":1:", ":2:"))
    with pytest.raises(ValueError):
        pubkey.parse_qr_payload("RELAYDECK-ENROLL:1:!!!")
    evil = pubkey.build_qr_payload("", "e", "C", "t")
    with pytest.raises(ValueError):
        pubkey.parse_qr_payload(evil + "x")
    with pytest.raises(ValueError):
        pubkey.build_qr_payload("https://gw.example/" + "p" * 480, "e", "C", "t")


def test_qr_never_carries_secrets() -> None:
    seed, line = pubkey.generate()
    payload = pubkey.build_qr_payload("https://gw.example", "enr_1", pubkey.mint_code(), "t")
    seed_b64 = base64.b64encode(seed).decode()
    assert seed.hex() not in payload
    assert seed_b64 not in payload
    assert seed_b64.rstrip("=") not in payload
    assert line.split()[1] not in payload
    assert "rdh_" not in payload and "rdt_" not in payload


def test_codes_are_unambiguous_and_unique() -> None:
    seen = {pubkey.mint_code() for _ in range(200)}
    assert len(seen) == 200
    allowed = set("ABCDEFGHJKMNPQRSTUVWXYZ23456789")
    assert all(set(code) <= allowed and len(code) == 12 for code in seen)
