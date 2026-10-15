"""RFC 8291 encryption, RFC 8292 VAPID JWT and the VAPID/topic key files.

Synthetic keys only, under a temporary state directory. No network.
"""

from __future__ import annotations

import hashlib
import json
import os
import stat
from pathlib import Path

import pytest
from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.asymmetric.utils import encode_dss_signature
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

from relaydeck.gateway import webpush

# RFC 8291 section 5 example, verbatim from the RFC.
RFC_PLAINTEXT = b"When I grow up, I want to be a watermelon"
RFC_AUTH = "BTBZMqHH6r4Tts7J_aSIgg"
RFC_UA_PUBLIC = (
    "BCVxsr7N_eNgVRqvHtD0zTZsEc6-VV-JvLexhqUzORcxaOzi6-AYWXvTBHm4bjyPjs7Vd8pZGH6SRpkNtoIAiw4"
)
RFC_AS_PRIVATE = "yfWPiYE-n46HLnH0KqZOF1fJJU3MYrct3AELtAQ-oRw"
RFC_SALT = "DGv6ra1nlYgDCS1FRnbzlw"
RFC_BODY = (
    "DGv6ra1nlYgDCS1FRnbzlwAAEABBBP4z9KsN6nGRTbVYI_c7VJSPQTBtkgcy27mlmlMoZIIgDll6e3vCYLocInmYWA"
    "mS6TlzAC8wEqKK6PBru3jl7A_yl95bQpu6cVPTpK4Mqgkf1CXztLVBSt2Ks3oZwbuwXPXLWyouBWLVWGNWQexSgSxsj_Qulcy4a-fN"
)


@pytest.fixture
def state(tmp_path: Path) -> Path:
    directory = tmp_path / "state"
    directory.mkdir(mode=0o700)
    return directory


def _rfc_private() -> ec.EllipticCurvePrivateKey:
    return ec.derive_private_key(
        int.from_bytes(webpush.b64u_decode(RFC_AS_PRIVATE), "big"), ec.SECP256R1()
    )


def _decrypt(body: bytes, ua_private: ec.EllipticCurvePrivateKey, auth: bytes) -> bytes:
    """The receiving side of RFC 8291, used to prove the round trip for any key pair."""
    salt, record_size, id_len = body[:16], body[16:20], body[20]
    assert int.from_bytes(record_size, "big") == 4096
    as_public = body[21 : 21 + id_len]
    ciphertext = body[21 + id_len :]
    ua_public = ua_private.public_key().public_bytes(
        encoding=serialization.Encoding.X962,
        format=serialization.PublicFormat.UncompressedPoint,
    )
    peer = ec.EllipticCurvePublicKey.from_encoded_point(ec.SECP256R1(), as_public)
    shared = ua_private.exchange(ec.ECDH(), peer)
    ikm = HKDF(
        algorithm=hashes.SHA256(),
        length=32,
        salt=auth,
        info=b"WebPush: info\x00" + ua_public + as_public,
    ).derive(shared)
    cek = HKDF(
        algorithm=hashes.SHA256(), length=16, salt=salt, info=b"Content-Encoding: aes128gcm\x00"
    ).derive(ikm)
    nonce = HKDF(
        algorithm=hashes.SHA256(), length=12, salt=salt, info=b"Content-Encoding: nonce\x00"
    ).derive(ikm)
    padded = AESGCM(cek).decrypt(nonce, ciphertext, None)
    assert padded[-1] == 0x02  # last-record delimiter
    return padded[:-1]


def test_rfc8291_section5_vector_is_reproduced_exactly() -> None:
    body = webpush.encrypt(
        RFC_PLAINTEXT,
        webpush.b64u_decode(RFC_UA_PUBLIC),
        webpush.b64u_decode(RFC_AUTH),
        as_private=_rfc_private(),
        salt=webpush.b64u_decode(RFC_SALT),
    )
    assert webpush.b64u(body) == RFC_BODY


def test_encrypted_body_decrypts_for_the_subscriber_and_fails_for_anyone_else() -> None:
    ua_private = ec.generate_private_key(ec.SECP256R1())
    ua_public = ua_private.public_key().public_bytes(
        encoding=serialization.Encoding.X962,
        format=serialization.PublicFormat.UncompressedPoint,
    )
    auth = b"A" * 16
    body = webpush.encrypt(b'{"t":"RelayDeck"}', ua_public, auth)
    assert _decrypt(body, ua_private, auth) == b'{"t":"RelayDeck"}'
    with pytest.raises(InvalidTag):
        _decrypt(body, ec.generate_private_key(ec.SECP256R1()), auth)
    with pytest.raises(InvalidTag):
        _decrypt(body, ua_private, b"B" * 16)


def test_encrypt_refuses_bad_keys_and_oversize_payloads() -> None:
    ua_public = webpush.b64u_decode(RFC_UA_PUBLIC)
    with pytest.raises(ValueError):
        webpush.encrypt(b"x", ua_public[:64], webpush.b64u_decode(RFC_AUTH))
    with pytest.raises(ValueError):
        webpush.encrypt(b"x", ua_public, b"short")
    with pytest.raises(ValueError):
        webpush.encrypt(
            b"x" * (webpush.MAX_PAYLOAD_BYTES + 1), ua_public, webpush.b64u_decode(RFC_AUTH)
        )


def test_vapid_jwt_has_the_rfc8292_claims_and_a_valid_es256_signature() -> None:
    key = ec.generate_private_key(ec.SECP256R1())
    token = webpush.vapid_jwt(
        key, "https://fcm.googleapis.com", "https://example.com/relaydeck", 1_000
    )
    header_b64, claims_b64, sig_b64 = token.split(".")
    header = json.loads(webpush.b64u_decode(header_b64))
    claims = json.loads(webpush.b64u_decode(claims_b64))
    assert header == {"typ": "JWT", "alg": "ES256"}
    assert claims["aud"] == "https://fcm.googleapis.com"
    assert claims["sub"] == "https://example.com/relaydeck"
    assert claims["exp"] - 1_000 <= 24 * 60 * 60
    assert claims["exp"] > 1_000
    raw = webpush.b64u_decode(sig_b64)
    assert len(raw) == 64
    der = encode_dss_signature(int.from_bytes(raw[:32], "big"), int.from_bytes(raw[32:], "big"))
    key.public_key().verify(der, f"{header_b64}.{claims_b64}".encode(), ec.ECDSA(hashes.SHA256()))


def test_vapid_subject_must_be_a_routable_https_url() -> None:
    assert webpush.valid_subject("https://example.com/relaydeck")
    assert webpush.valid_subject("https://push.example.org/contact")
    assert not webpush.valid_subject("https://push.example.invalid/contact")
    assert not webpush.valid_subject("https://localhost/contact")
    assert not webpush.valid_subject("https://example.com/a b")


def test_the_vapid_key_is_created_once_with_private_permissions(state: Path) -> None:
    first = webpush.load_or_create_vapid(state)
    path = state / "webpush" / "vapid.pem"
    assert stat.S_IMODE(os.stat(path).st_mode) == 0o600
    assert stat.S_IMODE(os.stat(state / "webpush").st_mode) == 0o700
    second = webpush.load_or_create_vapid(state)
    assert webpush.public_key_b64u(first) == webpush.public_key_b64u(second)


def test_a_valid_key_is_never_overwritten_on_restart(state: Path) -> None:
    key = webpush.load_or_create_vapid(state)
    before = (state / "webpush" / "vapid.pem").read_bytes()
    webpush.load_or_create_vapid(state)
    webpush.load_or_create_vapid(state)
    assert (state / "webpush" / "vapid.pem").read_bytes() == before
    assert webpush.public_key_b64u(key) == webpush.public_key_b64u(
        webpush.load_or_create_vapid(state)
    )


def test_a_symlinked_key_file_is_refused(state: Path, tmp_path: Path) -> None:
    webpush.webpush_dir(state)
    target = tmp_path / "elsewhere.pem"
    target.write_bytes(
        ec.generate_private_key(ec.SECP256R1()).private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.PKCS8,
            encryption_algorithm=serialization.NoEncryption(),
        )
    )
    os.symlink(target, state / "webpush" / "vapid.pem")
    with pytest.raises(PermissionError):
        webpush.load_or_create_vapid(state)


def test_a_key_file_owned_by_another_user_is_refused(
    state: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    webpush.load_or_create_vapid(state)
    other = os.getuid() + 1
    monkeypatch.setattr(webpush.os, "getuid", lambda: other)
    with pytest.raises(PermissionError):
        webpush.load_or_create_vapid(state)


def test_a_wrong_curve_key_is_refused(state: Path) -> None:
    webpush.webpush_dir(state)
    key = ec.generate_private_key(ec.SECP384R1())
    (state / "webpush" / "vapid.pem").write_bytes(
        key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
    )
    with pytest.raises(ValueError):
        webpush.load_or_create_vapid(state)


def test_rotation_replaces_the_key_and_leaves_the_topic_key_alone(state: Path) -> None:
    old = webpush.public_key_b64u(webpush.load_or_create_vapid(state))
    topic = webpush.load_or_create_topic_key(state)
    new = webpush.public_key_b64u(webpush.rotate_vapid(state))
    assert new != old
    assert webpush.public_key_b64u(webpush.load_or_create_vapid(state)) == new
    assert webpush.load_or_create_topic_key(state) == topic


def test_the_topic_key_is_32_random_bytes_and_stable(state: Path) -> None:
    first = webpush.load_or_create_topic_key(state)
    assert len(first) == 32
    assert webpush.load_or_create_topic_key(state) == first
    assert hashlib.sha256(first).digest() != first
