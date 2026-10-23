"""Explicitly configured FCM HTTP v1 provider, using existing HTTP/crypto dependencies.

Sources: firebase.google.com/docs/cloud-messaging/send/v1-api and
firebase.google.com/docs/cloud-messaging/error-codes. No network at construction.
"""

from __future__ import annotations

import base64
import json
import re
import threading
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

import httpx
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa

from relaydeck.companion.private import read_private
from relaydeck.gateway.push import HINT_CATEGORIES, HINT_KEYS, ProviderOutcome, ProviderPort

TOKEN_URL = "https://oauth2.googleapis.com/token"  # noqa: S105 - URL
FCM_SCOPE = "https://www.googleapis.com/auth/firebase.messaging"


def _encoded(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")


class FcmProvider(ProviderPort):
    """Owner-selected service account; fixed HTTPS authorities, generic notification text."""

    def __init__(
        self,
        credentials: dict[str, Any],
        *,
        client: httpx.Client | None = None,
        clock: Callable[[], float] = time.time,
    ) -> None:
        project = credentials.get("project_id")
        email = credentials.get("client_email")
        if (
            credentials.get("type") != "service_account"
            or not isinstance(project, str)
            or re.fullmatch(r"[a-z][a-z0-9-]{4,28}[a-z0-9]", project) is None
            or not isinstance(email, str)
            or re.fullmatch(r"[A-Za-z0-9._-]+@[A-Za-z0-9.-]+\.iam\.gserviceaccount\.com", email)
            is None
            or credentials.get("token_uri", TOKEN_URL) != TOKEN_URL
        ):
            raise ValueError("Invalid FCM service-account configuration")
        try:
            key = serialization.load_pem_private_key(
                credentials["private_key"].encode(), password=None
            )
        except (KeyError, AttributeError, ValueError, TypeError):
            raise ValueError("Invalid FCM signing key") from None
        if not isinstance(key, rsa.RSAPrivateKey) or key.key_size < 2048:
            raise ValueError("FCM requires an RSA signing key of at least 2048 bits")
        self.project = project
        self._email = email
        self._key = key
        self._client = client or httpx.Client(timeout=10.0, follow_redirects=False)
        self._owns_client = client is None
        self._clock = clock
        self._token = ""
        self._expires = 0.0
        self._lock = threading.Lock()

    @classmethod
    def from_file(cls, path: Path, **kwargs: Any) -> FcmProvider:
        try:
            doc = json.loads(read_private(path))
        except (OSError, ValueError):
            raise ValueError("FCM credentials require an owner-only regular JSON file") from None
        if not isinstance(doc, dict):
            raise ValueError("FCM credentials must be a service-account object")
        return cls(doc, **kwargs)

    def _access_token(self) -> str:
        with self._lock:
            now = self._clock()
            if self._token and now < self._expires - 60:
                return self._token
            header = _encoded(b'{"alg":"RS256","typ":"JWT"}')
            claims = _encoded(
                json.dumps(
                    {
                        "iss": self._email,
                        "scope": FCM_SCOPE,
                        "aud": TOKEN_URL,
                        "iat": int(now),
                        "exp": int(now) + 3600,
                    },
                    separators=(",", ":"),
                ).encode()
            )
            unsigned = f"{header}.{claims}"
            signature = self._key.sign(unsigned.encode(), padding.PKCS1v15(), hashes.SHA256())
            response = self._client.post(
                TOKEN_URL,
                data={
                    "grant_type": "urn:ietf:params:oauth:grant-type:jwt-bearer",
                    "assertion": f"{unsigned}.{_encoded(signature)}",
                },
            )
            if response.status_code != 200:
                return ""
            doc = response.json()
            if (
                not isinstance(doc, dict)
                or not isinstance(doc.get("access_token"), str)
                or not doc["access_token"]
                or doc.get("token_type", "Bearer").lower() != "bearer"
                or type(doc.get("expires_in")) is not int
                or not 60 < doc["expires_in"] <= 3600
            ):
                return ""
            self._token = doc["access_token"]
            self._expires = now + doc["expires_in"]
            return self._token

    def send(self, token: str, payload: dict[str, str]) -> ProviderOutcome:
        if (
            set(payload) != HINT_KEYS
            or payload.get("category") not in HINT_CATEGORIES
            or any(
                not isinstance(value, str) or not value or len(value) > 256
                for value in payload.values()
            )
            or not token
            or len(token) > 4096
        ):
            # A malformed message does not establish that a device token is invalid.
            return ProviderOutcome.RETRYABLE
        try:
            access = self._access_token()
            if not access:
                return ProviderOutcome.RETRYABLE
            response = self._client.post(
                f"https://fcm.googleapis.com/v1/projects/{self.project}/messages:send",
                headers={"Authorization": f"Bearer {access}"},
                json={
                    "message": {
                        "token": token,
                        "data": payload,
                        "notification": {
                            "title": "RelayDeck",
                            "body": "Open RelayDeck to view the update.",
                        },
                        "android": {"priority": "high", "ttl": "60s"},
                    }
                },
            )
            doc = response.json()
            if (
                response.status_code == 200
                and isinstance(doc, dict)
                and isinstance(doc.get("name"), str)
                and doc["name"]
            ):
                return ProviderOutcome.SENT
            if response.status_code == 401:
                with self._lock:
                    self._token = ""
                    self._expires = 0
            error = doc.get("error", {}) if isinstance(doc, dict) else {}
            details = error.get("details", []) if isinstance(error, dict) else []
            if isinstance(details, list) and any(
                isinstance(item, dict)
                and item.get("@type") == "type.googleapis.com/google.firebase.fcm.v1.FcmError"
                and item.get("errorCode") == "UNREGISTERED"
                for item in details
            ):
                return ProviderOutcome.PERMANENT
        except (httpx.HTTPError, ValueError, TypeError, AttributeError):
            pass  # Generic retry outcome; never include provider bodies, tokens or assertions.
        return ProviderOutcome.RETRYABLE

    def close(self) -> None:
        with self._lock:
            self._token = ""
            self._expires = 0
        if self._owns_client:
            self._client.close()
