"""Reviewed OIDC code boundary; fixed owner configuration, never client-supplied grants.

Source: OpenID Connect Core 1.0 errata2, sections3.1.2/3.1.3.7.
The supported profile is HTTPS authorization code + PKCE, RS256 ID tokens.
"""

from __future__ import annotations

import base64
import hashlib
import json
import re
import secrets
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol
from urllib.parse import urlencode, urlsplit

import httpx
from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import padding, rsa

from relaydeck.companion.private import read_private
from relaydeck.contracts import paths
from relaydeck.contracts.schemas import CAPABILITIES, SchemaError, validate
from relaydeck.gateway.identity import pkce_challenge

NATIVE_CALLBACK = "dev.relaydeck.app://oauth"
FLOW_LIMIT = 256
FLOW_TTL_MS = 300_000
CODE_TTL_MS = 60_000


class AuthorizationError(ValueError):
    pass


@dataclass(frozen=True)
class Principal:
    account: str
    capabilities: frozenset[str]
    hosts: frozenset[str]


@dataclass(frozen=True)
class Attempt:
    client_state: str
    challenge: str
    redirect_uri: str
    nonce: str
    verifier: str
    callback: str
    expires_ms: int


class IdentityProvider(Protocol):
    def authorize_url(self, state: str, attempt: Attempt) -> str: ...
    async def exchange(self, code: str, attempt: Attempt) -> Principal: ...


class AuthorizationFlow:
    def __init__(self) -> None:
        self.pending: dict[str, Attempt] = {}

    def begin(self, query: dict[str, str], origin: str, now: int) -> tuple[str, Attempt]:
        try:
            validate("relaydeck/gateway/v1/authorization-request", query)
        except SchemaError:
            raise AuthorizationError("Invalid authorization request") from None
        parsed = urlsplit(origin)
        if (
            parsed.scheme != "https"
            or not parsed.hostname
            or parsed.path not in ("", "/")
            or parsed.username
            or parsed.password
            or parsed.query
            or parsed.fragment
        ):
            raise AuthorizationError("Private HTTPS origin required")
        base = origin.rstrip("/")
        redirect = query.get("redirect_uri", "")
        permitted = {NATIVE_CALLBACK, base + "/app/oauth", base + paths.AUTH_CALLBACK}
        if (
            set(query)
            - {"response_type", "code_challenge", "code_challenge_method", "state", "redirect_uri"}
            or query.get("response_type") != "code"
            or query.get("code_challenge_method") != "S256"
            or re.fullmatch(r"[A-Za-z0-9_-]{43}", query.get("code_challenge", "")) is None
            or re.fullmatch(r"[A-Za-z0-9_-]{16,128}", query.get("state", "")) is None
            or redirect not in permitted
        ):
            raise AuthorizationError("Invalid authorization request")
        self.pending = {key: value for key, value in self.pending.items() if value.expires_ms > now}
        if len(self.pending) >= FLOW_LIMIT:
            raise AuthorizationError("Authorization capacity reached")
        state = secrets.token_urlsafe(32)
        attempt = Attempt(
            query["state"],
            query["code_challenge"],
            redirect,
            secrets.token_urlsafe(32),
            secrets.token_urlsafe(48),
            base + paths.IDENTITY_CALLBACK,
            now + FLOW_TTL_MS,
        )
        self.pending[state] = attempt
        return state, attempt

    def take(self, state: str, now: int) -> Attempt:
        attempt = self.pending.pop(state, None)
        if attempt is None or attempt.expires_ms <= now:
            raise AuthorizationError("Authorization expired or already consumed")
        return attempt


def _https(value: Any) -> str:
    if not isinstance(value, str) or len(value) > 2048:
        raise AuthorizationError("Invalid identity endpoint")
    parsed = urlsplit(value)
    if (
        parsed.scheme != "https"
        or not parsed.hostname
        or parsed.username
        or parsed.password
        or parsed.fragment
        or parsed.query
    ):
        raise AuthorizationError("Identity endpoints require HTTPS without credentials")
    return value


def _decode(value: str) -> bytes:
    if not value or re.fullmatch(r"[A-Za-z0-9_-]+", value) is None:
        raise AuthorizationError("Malformed identity token")
    return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))


def _unique(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise AuthorizationError("Duplicate identity field")
        result[key] = value
    return result


class OidcProvider:
    def __init__(
        self,
        config: dict[str, Any],
        *,
        client: httpx.AsyncClient | None = None,
        clock: Callable[[], int],
    ) -> None:
        if set(config) - {
            "issuer",
            "authorization_endpoint",
            "token_endpoint",
            "jwks_uri",
            "client_id",
            "client_secret",
            "grants",
        }:
            raise AuthorizationError("Unknown identity configuration field")
        self.issuer = _https(config.get("issuer"))
        self.authorization_endpoint = _https(config.get("authorization_endpoint"))
        self.token_endpoint = _https(config.get("token_endpoint"))
        self.jwks_uri = _https(config.get("jwks_uri"))
        self.client_id: str = config.get("client_id", "")
        self.client_secret = config.get("client_secret", "")
        if (
            not isinstance(self.client_id, str)
            or not 1 <= len(self.client_id) <= 256
            or not isinstance(self.client_secret, str)
            or len(self.client_secret) > 4096
        ):
            raise AuthorizationError("Invalid identity client configuration")
        self.client_id = str(self.client_id)
        grants = config.get("grants")
        if not isinstance(grants, dict) or not 1 <= len(grants) <= 256:
            raise AuthorizationError("Explicit subject grants required")
        self.grants: dict[str, Principal] = {}
        for subject, grant in grants.items():
            if (
                not isinstance(subject, str)
                or not 1 <= len(subject) <= 255
                or not isinstance(grant, dict)
                or set(grant) != {"account", "capabilities", "hosts"}
                or not isinstance(grant["account"], str)
                or not 1 <= len(grant["account"]) <= 128
            ):
                raise AuthorizationError("Invalid subject grants")
            for key in ("capabilities", "hosts"):
                if (
                    not isinstance(grant[key], list)
                    or len(grant[key]) > 128
                    or any(
                        not isinstance(item, str) or not 1 <= len(item) <= 128
                        for item in grant[key]
                    )
                ):
                    raise AuthorizationError("Invalid subject grants")
            self.grants[subject] = Principal(
                grant["account"], frozenset(grant["capabilities"]), frozenset(grant["hosts"])
            )
            if not self.grants[subject].capabilities <= frozenset(CAPABILITIES):
                raise AuthorizationError("Unknown granted capability")
        self.client = client or httpx.AsyncClient(timeout=10, follow_redirects=False)
        self.owns_client = client is None
        self.clock = clock

    @classmethod
    def from_file(cls, path: Path, clock: Callable[[], int]) -> OidcProvider:
        return cls(json.loads(read_private(path), object_pairs_hook=_unique), clock=clock)

    async def close(self) -> None:
        if self.owns_client:
            await self.client.aclose()

    def authorize_url(self, state: str, attempt: Attempt) -> str:
        return (
            self.authorization_endpoint
            + "?"
            + urlencode(
                {
                    "client_id": self.client_id,
                    "response_type": "code",
                    "scope": "openid",
                    "redirect_uri": attempt.callback,
                    "state": state,
                    "nonce": attempt.nonce,
                    "code_challenge": pkce_challenge(attempt.verifier),
                    "code_challenge_method": "S256",
                }
            )
        )

    async def _json(self, method: str, url: str, **kwargs: Any) -> dict[str, Any]:
        try:
            async with self.client.stream(
                method, url, timeout=10, follow_redirects=False, **kwargs
            ) as response:
                if response.status_code != 200:
                    raise AuthorizationError("Identity provider rejected authorization")
                body = bytearray()
                async for piece in response.aiter_bytes():
                    body.extend(piece)
                    if len(body) > 65536:
                        raise AuthorizationError("Identity response too large")
                value = json.loads(body, object_pairs_hook=_unique)
                if not isinstance(value, dict):
                    raise AuthorizationError("Invalid identity response")
                return value
        except (httpx.HTTPError, ValueError, TypeError):
            raise AuthorizationError("Identity exchange unavailable; start a new sign-in") from None

    async def exchange(self, code: str, attempt: Attempt) -> Principal:
        data = {
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": attempt.callback,
            "client_id": self.client_id,
            "code_verifier": attempt.verifier,
        }
        auth = httpx.BasicAuth(self.client_id, self.client_secret) if self.client_secret else None
        token = await self._json("POST", self.token_endpoint, data=data, auth=auth)
        jwks = await self._json("GET", self.jwks_uri)
        try:
            encoded = token["id_token"]
            if not isinstance(encoded, str) or len(encoded) > 16384:
                raise AuthorizationError("Invalid identity token")
            head, payload, signature = encoded.split(".")
            header = json.loads(_decode(head), object_pairs_hook=_unique)
            claims = json.loads(_decode(payload), object_pairs_hook=_unique)
            if (
                not isinstance(header, dict)
                or not isinstance(claims, dict)
                or header.get("alg") != "RS256"
                or not isinstance(header.get("kid"), str)
                or "crit" in header
            ):
                raise AuthorizationError("Unsupported identity signature")
            keys = jwks["keys"]
            if not isinstance(keys, list) or len(keys) > 32:
                raise AuthorizationError("Invalid identity keys")
            matches = [
                key
                for key in keys
                if isinstance(key, dict)
                and key.get("kid") == header["kid"]
                and key.get("kty") == "RSA"
                and key.get("use", "sig") == "sig"
                and key.get("alg", "RS256") == "RS256"
            ]
            if len(matches) != 1:
                raise AuthorizationError("Identity key not found")
            key = matches[0]
            if (
                not isinstance(key.get("n"), str)
                or not 1 <= len(key["n"]) <= 684
                or not isinstance(key.get("e"), str)
                or not 1 <= len(key["e"]) <= 8
            ):
                raise AuthorizationError("Invalid identity key size")
            public = rsa.RSAPublicNumbers(
                int.from_bytes(_decode(key["e"])), int.from_bytes(_decode(key["n"]))
            ).public_key()
            if not 2048 <= public.key_size <= 4096:
                raise AuthorizationError("Unsupported identity key size")
            public.verify(
                _decode(signature),
                (head + "." + payload).encode(),
                padding.PKCS1v15(),
                hashes.SHA256(),
            )
            now = self.clock() // 1000
            audience = claims.get("aud")
            if (
                claims.get("iss") != self.issuer
                or audience not in (self.client_id, [self.client_id])
                or claims.get("azp", self.client_id) != self.client_id
                or type(claims.get("exp")) is not int
                or claims["exp"] <= now
                or type(claims.get("iat")) is not int
                or not now - 600 <= claims["iat"] <= now + 30
                or not isinstance(claims.get("nonce"), str)
                or not secrets.compare_digest(claims["nonce"], attempt.nonce)
                or not isinstance(claims.get("sub"), str)
            ):
                raise AuthorizationError("Identity claims rejected")
            if "nbf" in claims and (type(claims["nbf"]) is not int or claims["nbf"] > now + 30):
                raise AuthorizationError("Identity token not yet valid")
            if "at_hash" in claims:
                expected = (
                    base64.urlsafe_b64encode(
                        hashlib.sha256(token["access_token"].encode("ascii")).digest()[:16]
                    )
                    .rstrip(b"=")
                    .decode()
                )
                if claims["at_hash"] != expected:
                    raise AuthorizationError("Identity access hash mismatch")
            principal = self.grants.get(claims["sub"])
            if principal is None:
                raise AuthorizationError("Identity subject is not authorized")
            return principal
        except (KeyError, ValueError, TypeError, AttributeError, InvalidSignature):
            raise AuthorizationError("Identity validation rejected") from None
