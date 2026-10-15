"""Management intent, authority fences and one-use enrollment state; no Flet."""

from __future__ import annotations

import asyncio
import time
from collections.abc import Callable
from datetime import datetime
from typing import Any

from relaydeck.client.features.sync.transport import HttpxGatewayTransport, TransportError
from relaydeck.contracts.pubkeys import fingerprint_of_raw, openssh_to_raw, raw_to_openssh
from relaydeck.contracts.schemas import SchemaError, validate


class ManagementController:
    def __init__(
        self,
        transport: HttpxGatewayTransport,
        account: str,
        notify: Callable[[], Any],
        on_hosts: Callable[[list[dict[str, Any]]], Any],
        on_logout: Callable[[], Any],
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.transport = transport
        self.account = account
        self.notify = notify
        self.on_hosts = on_hosts
        self.on_logout = on_logout
        self.clock = clock
        self.generation = 0
        self.revision = 0
        self.alive = True
        self.busy = False
        self.needs_refresh = False
        self.error = ""
        self.notice = ""
        self.capabilities: frozenset[str] = frozenset()
        self.current_device_id = ""
        self.hosts: list[dict[str, Any]] = []
        self.devices: list[dict[str, Any]] = []
        self.truncated = False
        self.invitation: dict[str, Any] = {}

    def clear(self) -> None:
        self.generation += 1
        self.hosts = []
        self.devices = []
        self.invitation = {}
        self.current_device_id = ""
        self.capabilities = frozenset()
        self.busy = False
        self.notice = ""

    def dispose(self) -> None:
        self.alive = False
        self.clear()

    def _live(self, generation: int) -> bool:
        return self.alive and generation == self.generation

    def _failure(self, exc: TransportError) -> None:
        if exc.code in {"unauthenticated", "forbidden"}:
            self.clear()
        # Server messages may contain sensitive request context; expose stable codes only.
        self.error = exc.code
        self.notify()
        if exc.code == "unauthenticated":
            self.on_logout()

    def expire(self) -> None:
        if not self.invitation:
            return
        expiry = datetime.fromisoformat(self.invitation["expires_at"].replace("Z", "+00:00"))
        if expiry.timestamp() <= self.clock():
            self.invitation = {}
            self.notice = "Enrollment expired. Create a new invitation."
            self.notify()

    async def _read(self, generation: int) -> None:
        me, hosts, devices = await asyncio.gather(
            self.transport.me(), self.transport.hosts(), self.transport.devices()
        )
        try:
            validate("relaydeck/gateway/v1/me", me)
            validate("relaydeck/gateway/v1/hosts", hosts)
            validate("relaydeck/gateway/v1/devices", devices)
        except SchemaError:
            raise TransportError(
                "upstream_incompatible", "management metadata invalid", False
            ) from None
        if not self._live(generation):
            return
        if me["account"] != self.account or devices["current_device_id"] != me["device_id"]:
            raise TransportError("unauthenticated", "management principal changed", False)
        if self.invitation:
            try:
                status = await self.transport.enrollment_status(self.invitation["id"])
            except TransportError as exc:
                if exc.code != "not_found":
                    raise
                status = {"status": "expired"}
            if not self._live(generation):
                return
            if status["status"] != "pending":
                self.invitation = {}
                self.notice = (
                    "Enrollment redeemed. Reachability and protocol status are shown separately."
                    if status["status"] == "claimed"
                    else "Enrollment expired or exhausted. Create a new invitation."
                )
        self.capabilities = frozenset(me["capabilities"])
        self.current_device_id = me["device_id"]
        self.hosts = hosts["hosts"]
        self.devices = devices["devices"]
        self.truncated = devices["truncated"]
        self.revision += 1
        self.needs_refresh = False
        self.expire()
        self.on_hosts(self.hosts)
        self.notify()

    async def refresh(self) -> None:
        if not self.alive or self.busy:
            return
        self.busy = True
        self.error = ""
        generation = self.generation
        self.notify()
        try:
            await self._read(generation)
        except TransportError as exc:
            if self._live(generation):
                self._failure(exc)
        finally:
            if self._live(generation):
                self.busy = False
                self.notify()

    async def enroll(self, label: str, public_key: str, expected: str, confirmed: bool) -> None:
        if (
            not self.alive
            or self.busy
            or self.needs_refresh
            or "hosts.enroll" not in self.capabilities
        ):
            return
        try:
            raw = openssh_to_raw(public_key)
            fingerprint = fingerprint_of_raw(raw)
            if not confirmed or expected.strip() != fingerprint:
                raise ValueError("fingerprint")
            body = {"host_label": label.strip(), "pubkey": raw_to_openssh(raw)}
            validate("relaydeck/gateway/v1/enrollment-create", body)
        except ValueError:
            self.error = "Confirm the full fingerprint from your Mac and a valid host label/key."
            self.notify()
            return
        self.busy = True
        self.error = ""
        generation = self.generation
        self.notify()
        try:
            invitation = await self.transport.create_enrollment(body["host_label"], body["pubkey"])
            if not self._live(generation):
                return
            if invitation["fingerprint"] != fingerprint:
                raise TransportError("upstream_incompatible", "fingerprint changed", False)
            self.invitation = invitation
            self.notice = "One-use invitation ready. Pairing does not establish connectivity."
        except TransportError as exc:
            if self._live(generation):
                self._failure(exc)
                if exc.code == "host_offline":
                    self.needs_refresh = True
                    self.notice = "Outcome unknown. Refresh before trying again."
        finally:
            if self._live(generation):
                self.busy = False
                self.notify()

    async def revoke(self, kind: str, identity: str, confirmed: bool) -> None:
        if not self.alive or self.busy or self.needs_refresh or not confirmed:
            return
        rows = self.devices if kind == "device" else self.hosts if kind == "host" else []
        key = "device_id" if kind == "device" else "host_id"
        if not any(row[key] == identity for row in rows):
            return
        if kind == "host" and "hosts.enroll" not in self.capabilities:
            return
        self.busy = True
        self.error = ""
        generation = self.generation
        self.notify()
        try:
            operation = (
                self.transport.revoke_device if kind == "device" else self.transport.revoke_host
            )
            await operation(identity)
            if not self._live(generation):
                return
            if kind == "device" and identity == self.current_device_id:
                self.clear()
                self.notify()
                self.on_logout()
                return
            self.notice = "Access revoked. The remote process is not terminated."
            await self._read(generation)
        except TransportError as exc:
            if self._live(generation):
                self._failure(exc)
                if exc.code == "host_offline":
                    self.needs_refresh = True
                    self.notice = "Outcome unknown. Refresh before trying again."
        finally:
            if self._live(generation):
                self.busy = False
                self.notify()
