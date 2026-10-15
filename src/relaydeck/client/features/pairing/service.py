"""Flet-free, generation-fenced device pairing. No write/redemption replay."""

from __future__ import annotations

import asyncio
import base64
import hashlib
import logging
import secrets
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from typing import Any

from relaydeck.client.adapters.ports import CredentialStore
from relaydeck.contracts.endpoints import origin
from relaydeck.contracts.pairing import DEVICE_CAPABILITIES, MAX_POLLS, REQUEST_TTL_MS
from relaydeck.contracts.schemas import validate

PENDING_KEY = "pairing_pending_key"

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class PairingSnapshot:
    endpoint: str = ""
    host_id: str = "home-mac"
    device_label: str = "My device"
    phase: str = "setup"
    message: str = "Open a pairing window on your Mac, then compare the matching code."
    request_id: str = ""
    matching_code: str = ""
    fingerprint: str = ""
    scope: str = ""
    expiry: str = ""
    busy: bool = False


async def discard_pending(credentials: CredentialStore) -> None:
    """Process death never resumes a pending exchange or redeems an old request."""
    key = await credentials.get(PENDING_KEY)
    if key and key.startswith("pairing_verifier_"):
        await credentials.delete(key)
    await credentials.delete(PENDING_KEY)


class PairingFlow:
    def __init__(
        self,
        credentials: CredentialStore,
        post: Callable[..., Awaitable[Any]],
        get: Callable[..., Awaitable[Any]],
        accept: Callable[[dict[str, Any], dict[str, Any], Callable[[], bool]], Awaitable[bool]],
        *,
        now_ms: Callable[[], int] = lambda: int(time.time() * 1000),
        sleep: Callable[[float], Awaitable[Any]] = asyncio.sleep,
    ) -> None:
        self.credentials, self.post, self.get, self.accept = credentials, post, get, accept
        self.now_ms, self.sleep = now_ms, sleep
        self.snapshot = PairingSnapshot()
        self.generation = 0
        self.listeners: list[Callable[[PairingSnapshot], Any]] = []
        self._material_lock = asyncio.Lock()

    async def _clear_material(self, key: str) -> None:
        async with self._material_lock:
            await self.credentials.delete(key)
            if await self.credentials.get(PENDING_KEY) == key:
                await self.credentials.delete(PENDING_KEY)

    def emit(self, snapshot: PairingSnapshot) -> None:
        self.snapshot = snapshot
        for listener in tuple(self.listeners):
            try:
                listener(snapshot)
            except Exception:  # noqa: BLE001 - a dead page must not break others
                # A listener whose page/session is already gone (browser
                # closed or reconnected) is dropped; raising here crashed
                # the NEW page that triggered the cleanup.
                logger.debug("dropping failed pairing listener", exc_info=True)
                if listener in self.listeners:
                    self.listeners.remove(listener)

    def subscribe(self, listener: Callable[[PairingSnapshot], Any]) -> Callable[[], None]:
        self.listeners.append(listener)
        return lambda: self.listeners.remove(listener) if listener in self.listeners else None

    def invalidate(self) -> None:
        self.generation += 1
        self.emit(
            replace(
                self.snapshot,
                phase="setup",
                busy=False,
                request_id="",
                matching_code="",
                fingerprint="",
                scope="",
                expiry="",
            )
        )

    async def cancel(self) -> None:
        self.invalidate()
        async with self._material_lock:
            await discard_pending(self.credentials)

    async def begin(self, endpoint: str, host_id: str, label: str) -> None:
        self.invalidate()
        generation = self.generation

        def current() -> bool:
            return self.generation == generation

        key = "pairing_verifier_" + secrets.token_urlsafe(24)
        cleared = False
        snapshot = PairingSnapshot(
            endpoint=endpoint, host_id=host_id, device_label=label, phase="connecting", busy=True
        )
        self.emit(snapshot)
        try:
            endpoint = origin(endpoint, tailscale=True)
            if not await self.credentials.available():
                raise ValueError("secure storage unavailable")
            verifier = secrets.token_urlsafe(48)
            challenge = (
                base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest())
                .decode()
                .rstrip("=")
            )
            body = {
                "origin": endpoint,
                "host_id": host_id,
                "device_label": label,
                "code_challenge": challenge,
                "code_challenge_method": "S256",
            }
            validate("relaydeck/gateway/v1/pairing-request", body)
            async with self._material_lock:
                if not current():
                    return
                await discard_pending(self.credentials)
                await self.credentials.set(key, verifier)
                if current():
                    await self.credentials.set(PENDING_KEY, key)
            if not current():
                return
            status, row = await self.post(endpoint + "/v1/pairing/requests", body)
            if not current():
                return
            if status != 201:
                raise ValueError("pairing window unavailable")
            validate("relaydeck/gateway/v1/pairing-status", row)
            if (
                row["origin"] != endpoint
                or row["host_id"] != host_id
                or row["state"] != "pending"
                or sorted(row["capabilities"]) != sorted(DEVICE_CAPABILITIES)
            ):
                raise ValueError("pairing binding mismatch")
            expires = int(
                datetime.strptime(row["expires_at"], "%Y-%m-%dT%H:%M:%SZ")
                .replace(tzinfo=UTC)
                .timestamp()
                * 1000
            )
            if not self.now_ms() < expires <= self.now_ms() + REQUEST_TTL_MS:
                raise ValueError("expired pairing request")
            snapshot = replace(
                snapshot,
                endpoint=endpoint,
                phase="waiting",
                busy=False,  # waiting on local approval remains explicitly cancellable
                request_id=row["request_id"],
                matching_code=row["matching_code"],
                fingerprint=row["host_fingerprint"],
                scope=", ".join(row["capabilities"]),
                expiry=row["expires_at"],
                message="Compare code, fingerprint and scope, then approve locally on your Mac.",
            )
            self.emit(snapshot)
            expected = {k: v for k, v in row.items() if k != "state"}
            proof = {"origin": endpoint, "host_id": host_id, "verifier": verifier}
            for _ in range(MAX_POLLS):
                await self.sleep(row["poll_interval_ms"] / 1000)
                if not current():
                    return
                if self.now_ms() >= expires:
                    raise ValueError("pairing expired")
                status, polled = await self.post(
                    endpoint + f"/v1/pairing/{row['request_id']}/status", proof
                )
                if not current():
                    return
                if status != 200:
                    raise ValueError("pairing expired or unavailable")
                validate("relaydeck/gateway/v1/pairing-status", polled)
                if {k: v for k, v in polled.items() if k != "state"} != expected:
                    raise ValueError("pairing response changed authority")
                if polled["state"] == "denied":
                    raise ValueError("pairing denied")
                if polled["state"] == "approved":
                    break
            else:
                raise ValueError("pairing polling exhausted")
            if self.now_ms() >= expires:
                raise ValueError("pairing expired")
            self.emit(
                replace(
                    snapshot,
                    phase="approved",
                    message="Mac approved this device. Establishing a protected session.",
                )
            )
            status, tokens = await self.post(
                endpoint + f"/v1/pairing/{row['request_id']}/redeem", proof
            )
            if not current():
                return
            if status != 200:
                raise ValueError("redemption uncertain; start a new pairing attempt")
            validate("relaydeck/gateway/v1/token-response", tokens)
            status, principal = await self.get(
                endpoint + "/v1/me", {"Authorization": f"Bearer {tokens['access_token']}"}
            )
            if not current():
                return
            validate("relaydeck/gateway/v1/me", principal)
            if (
                status != 200
                or principal["device_id"] != row["device_id"]
                or principal["hosts"] != [host_id]
                or sorted(principal["capabilities"]) != sorted(DEVICE_CAPABILITIES)
            ):
                raise ValueError("issued session binding mismatch")
            # Cleanup must succeed before either adapter publishes credentials.
            await self._clear_material(key)
            cleared = True
            if not current():
                return
            if await self.accept(tokens, principal, current) and current():
                self.emit(
                    replace(
                        snapshot,
                        phase="ready",
                        busy=False,
                        message="Device authorized. Opening Console.",
                    )
                )
            elif current():
                raise ValueError("credentials could not be stored")
        except asyncio.CancelledError:
            if current():
                self.invalidate()
            raise
        except Exception:  # noqa: BLE001 - never reflect tokens, labels or network exception bodies
            if current():
                self.emit(
                    replace(
                        self.snapshot,
                        phase="failed",
                        busy=False,
                        message=(
                            "Connection unavailable, denied or expired. Check private HTTPS and "
                            "connector enrollment; run relaydeck pairing open on the Mac, "
                            "then explicitly "
                            "start again. Uncertain redemption is never replayed."
                        ),
                    )
                )
        finally:
            try:
                if not cleared:
                    await self._clear_material(key)
            except Exception:  # noqa: BLE001 - secure storage stays private; never downgrade
                if current():
                    self.emit(
                        replace(
                            self.snapshot,
                            phase="failed",
                            busy=False,
                            message="Secure storage cleanup failed. Restart before pairing again.",
                        )
                    )
