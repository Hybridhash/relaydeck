"""Web Push delivery (RFC 8030, RFC 8291 aes128gcm, RFC 8292 VAPID) for browser apps.

Security rules (security review), enforced here and tested in tests/unit/test_webpush_*.py:

* VAPID keys live in ``<state_dir>/webpush/vapid.pem`` (directory 0700, file 0600). A valid key is
  never overwritten on restart; creation is exclusive; symlinks and foreign-owned files are refused.
* Endpoints are allowlisted before any connection: HTTPS, port 443, no userinfo, no fragment, no IP
  literal, canonical (IDNA, lowercase) host compared exactly or by label-boundary suffix. Every
  resolved address must be public. The request connects to the validated address with no redirects,
  a 10 s timeout and a bounded response read.
* Nothing in this module logs endpoints, keys, or payloads.
"""

from __future__ import annotations

import base64
import ipaddress
import json
import logging
import os
import re
import secrets
import socket
import stat as stat_module
import struct
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit

import httpx
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.asymmetric.utils import decode_dss_signature
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

from relaydeck.contracts.canonical import canonical_json
from relaydeck.gateway.push import ProviderOutcome, ProviderPort

LOGGER = logging.getLogger(__name__)
# httpx logs every request URL at INFO. A push endpoint path is a per-device secret, so the request
# lines are kept below WARNING wherever this module is loaded.
logging.getLogger("httpx").setLevel(logging.WARNING)

VAPID_SUBJECT_DEFAULT = "https://example.com/relaydeck"
EXACT_HOSTS = frozenset(
    {"fcm.googleapis.com", "web.push.apple.com", "updates.push.services.mozilla.com"}
)
# Matched as ".<suffix>" so "evilnotify.windows.com" and "notify.windows.com.evil" both fail.
LABEL_SUFFIXES = ("notify.windows.com",)
MAX_PAYLOAD_BYTES = 2048
MAX_TTL_SECONDS = 24 * 60 * 60
MAX_RETRY_MS = 60 * 60 * 1000
URGENCIES = frozenset({"very-low", "low", "normal", "high"})
TIMEOUT_SECONDS = 10.0
RESPONSE_CAP_BYTES = 4096
JWT_LIFETIME_SECONDS = 12 * 60 * 60  # well inside the RFC 8292 24 h ceiling
RECORD_SIZE = 4096
_B64U_CHARS = frozenset("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_")


class EndpointRejected(Exception):
    """The endpoint fails the allowlist or the public-address check. Carries no URL."""


@dataclass(frozen=True)
class EndpointTarget:
    host: str  # canonical host: SNI, the Host header and the JWT audience
    path: str  # path and query, exactly as the subscription supplied them


def b64u(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")


def b64u_decode(value: str) -> bytes:
    if not value or any(char not in _B64U_CHARS for char in value):
        raise ValueError("not base64url")
    return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))


def canonical_host(host: str) -> str:
    try:
        return host.encode("idna").decode("ascii").lower()
    except UnicodeError as exc:
        raise EndpointRejected("host") from exc


def check_endpoint(url: str) -> EndpointTarget:
    """Allowlist check. Raises EndpointRejected for anything outside the policy, with no detail."""
    if not isinstance(url, str) or not url or len(url) > 2048:
        raise EndpointRejected("shape")
    try:
        parts = urlsplit(url)
        port = parts.port
    except ValueError as exc:
        raise EndpointRejected("parse") from exc
    if parts.scheme != "https" or parts.username is not None or parts.password is not None:
        raise EndpointRejected("scheme-or-userinfo")
    if parts.fragment or port not in (None, 443) or not parts.hostname:
        raise EndpointRejected("fragment-or-port")
    raw = parts.hostname
    try:
        ipaddress.ip_address(raw)
    except ValueError:
        pass
    else:
        raise EndpointRejected("ip-literal")
    host = canonical_host(raw)
    allowed = host in EXACT_HOSTS or any(host.endswith("." + suffix) for suffix in LABEL_SUFFIXES)
    if not allowed:
        raise EndpointRejected("host")
    path = parts.path or "/"
    if parts.query:
        path += "?" + parts.query
    return EndpointTarget(host=host, path=path)


def resolve_public(host: str) -> str:
    """Resolve once and require every address to be public. Returns the first address.

    A resolution failure is an OSError (retryable). A non-public address is EndpointRejected.
    """
    try:
        infos = socket.getaddrinfo(host, 443, type=socket.SOCK_STREAM)
    except socket.gaierror as exc:
        raise OSError("resolution failed") from exc
    addresses = [str(info[4][0]).split("%", 1)[0] for info in infos]
    if not addresses:
        raise EndpointRejected("no-address")
    for text in addresses:
        address = ipaddress.ip_address(text)
        if (
            not address.is_global
            or address.is_private
            or address.is_loopback
            or address.is_link_local
            or address.is_multicast
            or address.is_reserved
            or address.is_unspecified
        ):
            raise EndpointRejected("non-public-address")
    return addresses[0]


def _url_host(address: str) -> str:
    return f"[{address}]" if ":" in address else address


# -- VAPID keys and the topic key ------------------------------------------------------------------


def webpush_dir(state_dir: Path) -> Path:
    directory = state_dir / "webpush"
    try:
        status = os.lstat(directory)
    except FileNotFoundError:
        directory.mkdir(mode=0o700, parents=True)
        status = os.lstat(directory)
    if stat_module.S_ISLNK(status.st_mode) or not stat_module.S_ISDIR(status.st_mode):
        raise PermissionError("unsafe webpush directory")
    if status.st_uid != os.getuid():
        raise PermissionError("webpush directory is not owned by this user")
    if status.st_mode & 0o077:
        os.chmod(directory, 0o700)
    return directory


def _existing_private_file(path: Path) -> bool:
    """True when a regular file owned by this user exists; refuses symlinks and foreign files."""
    try:
        status = os.lstat(path)
    except FileNotFoundError:
        return False
    if stat_module.S_ISLNK(status.st_mode) or not stat_module.S_ISREG(status.st_mode):
        raise PermissionError("unsafe webpush file")
    if status.st_uid != os.getuid():
        raise PermissionError("webpush file is not owned by this user")
    if status.st_mode & 0o077:
        os.chmod(path, 0o600)
    return True


def _write_private(path: Path, data: bytes, *, replace: bool) -> None:
    """Writes a 0600 temp file, then either links it into place (never overwriting) or replaces."""
    temp = path.with_name(f".{path.name}.{secrets.token_hex(8)}.tmp")
    fd = os.open(temp, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        if replace:
            os.replace(temp, path)
        else:
            os.link(temp, path)  # FileExistsError when a key already exists: keep that key
    finally:
        if temp.exists():
            os.unlink(temp)


def _pem(key: ec.EllipticCurvePrivateKey) -> bytes:
    return key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    )


def _read_vapid(path: Path) -> ec.EllipticCurvePrivateKey:
    loaded = serialization.load_pem_private_key(path.read_bytes(), password=None)
    if not isinstance(loaded, ec.EllipticCurvePrivateKey) or not isinstance(
        loaded.curve, ec.SECP256R1
    ):
        raise ValueError("webpush key must be a P-256 EC key")
    return loaded


def load_or_create_vapid(state_dir: Path) -> ec.EllipticCurvePrivateKey:
    path = webpush_dir(state_dir) / "vapid.pem"
    if _existing_private_file(path):
        return _read_vapid(path)
    key = ec.generate_private_key(ec.SECP256R1())
    try:
        _write_private(path, _pem(key), replace=False)
    except FileExistsError:
        return _read_vapid(path)  # another start created it first
    return key


def rotate_vapid(state_dir: Path) -> ec.EllipticCurvePrivateKey:
    """Replaces the key atomically. Callers disable sending and drop subscriptions first."""
    path = webpush_dir(state_dir) / "vapid.pem"
    key = ec.generate_private_key(ec.SECP256R1())
    _write_private(path, _pem(key), replace=True)
    return key


def public_key_b64u(key: ec.EllipticCurvePrivateKey) -> str:
    raw = key.public_key().public_bytes(
        serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint
    )
    return b64u(raw)


def _read_topic(path: Path) -> bytes:
    value = b64u_decode(path.read_text(encoding="ascii").strip())
    if len(value) != 32:
        raise ValueError("webpush topic key must be 32 bytes")
    return value


def load_or_create_topic_key(state_dir: Path) -> bytes:
    path = webpush_dir(state_dir) / "topic.key"
    if _existing_private_file(path):
        return _read_topic(path)
    try:
        _write_private(path, b64u(secrets.token_bytes(32)).encode("ascii"), replace=False)
    except FileExistsError:
        pass
    return _read_topic(path)


# -- RFC 8291 aes128gcm ----------------------------------------------------------------------------


def encrypt(
    plaintext: bytes,
    ua_public: bytes,
    auth_secret: bytes,
    *,
    as_private: ec.EllipticCurvePrivateKey | None = None,
    salt: bytes | None = None,
) -> bytes:
    """RFC 8291 section 3.4: returns the aes128gcm body (header, then one record)."""
    if len(ua_public) != 65 or ua_public[0] != 4 or len(auth_secret) != 16:
        raise ValueError("bad subscription keys")
    if len(plaintext) > MAX_PAYLOAD_BYTES:
        raise ValueError("payload too large")
    as_key = as_private or ec.generate_private_key(ec.SECP256R1())
    as_public = as_key.public_key().public_bytes(
        serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint
    )
    peer = ec.EllipticCurvePublicKey.from_encoded_point(ec.SECP256R1(), ua_public)
    shared = as_key.exchange(ec.ECDH(), peer)
    key_info = b"WebPush: info\x00" + ua_public + as_public
    ikm = HKDF(algorithm=hashes.SHA256(), length=32, salt=auth_secret, info=key_info).derive(shared)
    record_salt = salt if salt is not None else secrets.token_bytes(16)
    cek = HKDF(
        algorithm=hashes.SHA256(),
        length=16,
        salt=record_salt,
        info=b"Content-Encoding: aes128gcm\x00",
    ).derive(ikm)
    nonce = HKDF(
        algorithm=hashes.SHA256(), length=12, salt=record_salt, info=b"Content-Encoding: nonce\x00"
    ).derive(ikm)
    ciphertext = AESGCM(cek).encrypt(nonce, plaintext + b"\x02", None)  # 0x02 marks the last record
    header = record_salt + struct.pack("!I", RECORD_SIZE) + bytes([len(as_public)]) + as_public
    return header + ciphertext


# -- RFC 8292 VAPID ----------------------------------------------------------------------


def vapid_jwt(key: ec.EllipticCurvePrivateKey, audience: str, subject: str, now: int) -> str:
    """ES256 JWT with aud (the push service origin), exp (at most 24 h away) and sub."""
    header = b64u(json.dumps({"typ": "JWT", "alg": "ES256"}, separators=(",", ":")).encode())
    claims = b64u(
        json.dumps(
            {"aud": audience, "exp": now + JWT_LIFETIME_SECONDS, "sub": subject},
            separators=(",", ":"),
        ).encode()
    )
    signing_input = f"{header}.{claims}".encode("ascii")
    der = key.sign(signing_input, ec.ECDSA(hashes.SHA256()))
    r, s = decode_dss_signature(der)
    return f"{header}.{claims}.{b64u(r.to_bytes(32, 'big') + s.to_bytes(32, 'big'))}"


def valid_subject(value: str) -> bool:
    """The same rule as the companion configuration (one definition, see companion.config)."""
    from relaydeck.companion.config import valid_vapid_subject

    return valid_vapid_subject(value)


# -- provider ------------------------------------------------------------------------------


REASON_RE = re.compile(r"[A-Za-z]{1,32}")


def _reason_word(body: bytes) -> str:
    """The provider's short `reason` word, e.g. BadJwtToken. Anything else is not reported.

    A JSON body with a string `reason` is read; otherwise the value itself may be a bare class name.
    """
    try:
        doc = json.loads(body)
    except ValueError:
        doc = None
    value = doc.get("reason") if isinstance(doc, dict) else None
    if value is None:
        try:
            value = body.decode("ascii")
        except UnicodeDecodeError:
            return "-"
    text = str(value)
    return text if REASON_RE.fullmatch(text) else "-"


def _outcome_for(status: int) -> ProviderOutcome:
    if 200 <= status < 300:
        return ProviderOutcome.SENT
    if status in (404, 410):
        return (
            ProviderOutcome.PERMANENT
        )  # the subscription is gone; the store drops that generation
    return ProviderOutcome.RETRYABLE


class WebPushProvider(ProviderPort):
    """Delivers one encrypted message to one subscription.

    ``token`` is the JSON subscription document ``{"endpoint", "p256dh", "auth", "ttl", "urgency",
    "topic"}``. It is held in memory only. ``payload`` is the message dict of short strings.
    """

    def __init__(
        self,
        key: ec.EllipticCurvePrivateKey,
        *,
        subject: str = VAPID_SUBJECT_DEFAULT,
        client: httpx.Client | None = None,
        clock: Callable[[], float] = time.time,
        resolver: Callable[[str], str] = resolve_public,
    ) -> None:
        if not valid_subject(subject):
            raise ValueError("invalid VAPID subject")
        self._key = key
        self._subject = subject
        self._clock = clock
        self._resolver = resolver
        self._owns_client = client is None
        self._client = client or httpx.Client(
            timeout=TIMEOUT_SECONDS, follow_redirects=False, trust_env=False
        )

    def send(self, token: str, payload: dict[str, str]) -> ProviderOutcome:
        """One message. A failed send logs one WARNING: provider host, HTTP status and the
        provider's short reason word. Never the endpoint path, keys, JWT or payload."""
        report: dict[str, str] = {}
        outcome = self._deliver(token, payload, report)
        if outcome is not ProviderOutcome.SENT:
            LOGGER.warning(
                "web push send failed host=%s status=%s reason=%s outcome=%s",
                report.get("host", "-"),
                report.get("status", "-"),
                report.get("reason", "-"),
                outcome.value,
            )
        return outcome

    def _deliver(
        self, token: str, payload: dict[str, str], report: dict[str, str]
    ) -> ProviderOutcome:
        try:
            sub = json.loads(token)
            if not isinstance(sub, dict):
                return ProviderOutcome.PERMANENT
            body_plain = canonical_json(payload).encode()
            if len(body_plain) > MAX_PAYLOAD_BYTES:
                return ProviderOutcome.PERMANENT
            ttl = int(sub["ttl"])
            urgency = str(sub["urgency"])
            topic = str(sub["topic"])
            if not 0 < ttl <= MAX_TTL_SECONDS or urgency not in URGENCIES or len(topic) > 32:
                return ProviderOutcome.PERMANENT
            ua_public = b64u_decode(str(sub["p256dh"]))
            auth = b64u_decode(str(sub["auth"]))
            if len(ua_public) != 65 or len(auth) != 16:
                return ProviderOutcome.PERMANENT
            target = check_endpoint(str(sub["endpoint"]))
            report["host"] = target.host
            address = self._resolver(target.host)
        except EndpointRejected:
            return ProviderOutcome.PERMANENT  # never an endpoint we contact
        except OSError:
            return ProviderOutcome.RETRYABLE  # resolution failed: try again later
        except (KeyError, TypeError, ValueError):
            return ProviderOutcome.PERMANENT
        try:
            body = encrypt(body_plain, ua_public, auth)
            jwt = vapid_jwt(self._key, f"https://{target.host}", self._subject, int(self._clock()))
            headers = {
                "Authorization": f"vapid t={jwt}, k={public_key_b64u(self._key)}",
                "Content-Encoding": "aes128gcm",
                "Content-Type": "application/octet-stream",
                "TTL": str(ttl),
                "Urgency": urgency,
                "Topic": topic,
                "Host": target.host,
            }
            # Connect to the validated address; TLS still verifies the canonical hostname (SNI).
            url = f"https://{_url_host(address)}{target.path}"
            with self._client.stream(
                "POST",
                url,
                headers=headers,
                content=body,
                extensions={"sni_hostname": target.host},
            ) as response:
                report["status"] = str(response.status_code)
                received = bytearray()
                for chunk in response.iter_bytes():
                    received.extend(chunk)
                    if len(received) > RESPONSE_CAP_BYTES:
                        break  # bounded read
                report["reason"] = _reason_word(bytes(received))
                return _outcome_for(response.status_code)
        except (httpx.HTTPError, OSError, ValueError) as exc:
            # The exception text can carry the URL, so only its class name is reported.
            report["reason"] = _reason_word(type(exc).__name__.encode())
            return ProviderOutcome.RETRYABLE

    def close(self) -> None:
        if self._owns_client:
            self._client.close()
