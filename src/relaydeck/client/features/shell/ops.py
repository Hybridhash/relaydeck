"""Typed gateway operations for terminal ownership.

Attach, takeover, lease, ticket, release and close use explicit HTTP operations.
Errors preserve gateway codes and owner disclosure for conflict handling."""

from __future__ import annotations

import hashlib
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import quote

import httpx

from relaydeck.client.open_timing import mark_current
from relaydeck.contracts import paths


class ShellOpError(Exception):
    """Typed ownership-operation failure. `code` is the gateway envelope
    code (`lease_conflict`, `lease_expired`, `not_found`, ...); `details`
    carries the envelope details (owner/holder disclosure) verbatim."""

    def __init__(
        self,
        code: str,
        message: str,
        details: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(f"{code}: {message}")
        self.code = code
        self.message = message
        self.details = dict(details or {})


@dataclass(frozen=True)
class AttachmentInfo:
    attachment_id: str
    host_id: str
    terminal_id: str
    kind: str
    generation: int
    epoch: str
    owner_device_id: str
    owner_label: str
    takeover_authorized: bool

    @classmethod
    def from_wire(cls, doc: dict[str, Any]) -> AttachmentInfo:
        owner = doc.get("owner", {})
        if not isinstance(owner, dict):
            owner = {}
        return cls(
            attachment_id=str(doc.get("attachment_id", "")),
            host_id=str(doc.get("host_id", "")),
            terminal_id=str(doc.get("terminal_id", "")),
            kind=str(doc.get("kind", "")),
            generation=int(doc.get("generation", 0)),
            epoch=str(doc.get("epoch", "")),
            owner_device_id=str(owner.get("device_id", "")),
            owner_label=str(owner.get("label", "")),
            takeover_authorized=bool(doc.get("takeover_authorized", False)),
        )

    @property
    def owner_display(self) -> str:
        """Human-readable owner for the conflict banner: the
        disclosed label, falling back to the device id, never empty."""
        return self.owner_label or self.owner_device_id or "another device"


def owner_display_from_details(details: dict[str, Any]) -> str:
    """Owner label from a `lease_conflict` envelope (`details.owner` or
    the control-lease `details.holder`). Never empty, never cross-account
    (the gateway scrubs before disclosure)."""
    owner = details.get("owner")
    if isinstance(owner, dict):
        label = str(owner.get("label", "") or "")
        device = str(owner.get("device_id", "") or "")
        if label or device:
            return label or device
    holder = details.get("holder")
    if isinstance(holder, dict):
        device = str(holder.get("device_id", "") or "")
        if device:
            return device
    return "another device"


@dataclass(frozen=True)
class LeaseInfo:
    lease_id: str
    attachment_id: str
    host_id: str
    terminal_id: str
    mode: str
    generation: int
    epoch: str
    expires_at: str

    @classmethod
    def from_wire(cls, doc: dict[str, Any]) -> LeaseInfo:
        return cls(
            lease_id=str(doc.get("lease_id", "")),
            attachment_id=str(doc.get("attachment_id", "")),
            host_id=str(doc.get("host_id", "")),
            terminal_id=str(doc.get("terminal_id", "")),
            mode=str(doc.get("mode", "")),
            generation=int(doc.get("generation", 0)),
            epoch=str(doc.get("epoch", "")),
            expires_at=str(doc.get("expires_at", "")),
        )


class ShellGatewayOps(ABC):
    """Ownership operations one terminal detail needs. Production uses
    `HttpxShellGatewayOps`; headless UI tests use `ScriptedShellGatewayOps`."""

    @property
    def picture_api(self) -> Any:
        return None

    @abstractmethod
    async def attach(
        self,
        host_id: str,
        terminal_id: str,
        kind: str,
        takeover: bool,
        cols: int = 80,
        rows: int = 24,
        *,
        expected_control_revision: str | None = None,
    ) -> AttachmentInfo: ...

    @abstractmethod
    async def acquire_lease(
        self,
        attachment_id: str,
        mode: str,
        *,
        expected_generation: int | None = None,
    ) -> LeaseInfo: ...

    async def control_holder(self, host_id: str, terminal_id: str) -> dict[str, Any]:
        """Read-only control state ({"held", "mine"}). Implementations that
        cannot tell raise, and callers treat that as "ask first"."""
        raise ShellOpError("unsupported_capability", "control holder unknown")

    @abstractmethod
    async def terminal_ticket(self, lease_id: str) -> str: ...

    @abstractmethod
    async def release_lease(self, lease_id: str) -> None: ...

    @abstractmethod
    async def close_attachment(self, attachment_id: str) -> None: ...

    @abstractmethod
    async def close_terminal(self, command: dict[str, Any]) -> dict[str, Any]:
        """Submit the session.close command. One attempt; an
        unknown outcome is shown to the owner and never replayed."""


@dataclass
class HttpxShellGatewayOps(ShellGatewayOps):
    """HTTP operations for explicit terminal ownership."""

    base_url: str
    token: Any  # async callable returning the access token
    client: httpx.AsyncClient
    timeout: float = 30.0
    native_picture_upload: bool = False  # Trusted entrypoint, never client Page metadata.

    @property
    def picture_api(self) -> Any:
        from relaydeck.client.features.terminal.pictures import PictureApi

        return PictureApi(self)

    async def _auth(self) -> dict[str, str]:
        value = await self.token()
        if not value:
            raise ShellOpError("unauthenticated", "no access token")
        return {"Authorization": f"Bearer {value}"}

    def _checked(self, response: httpx.Response) -> dict[str, Any]:
        if 200 <= response.status_code < 300:
            try:
                data = response.json()
            except ValueError as exc:
                raise ShellOpError("upstream_incompatible", "bad json") from exc
            if not isinstance(data, dict):
                raise ShellOpError("upstream_incompatible", "bad shape")
            return data
        try:
            data = response.json()
        except ValueError:
            data = {}
        if not isinstance(data, dict):
            data = {}
        code = str(data.get("code", "upstream_incompatible"))
        message = str(data.get("message", "gateway error"))
        details = data.get("details")
        raise ShellOpError(code, message, details if isinstance(details, dict) else {})

    async def attach(
        self,
        host_id: str,
        terminal_id: str,
        kind: str,
        takeover: bool,
        cols: int = 80,
        rows: int = 24,
        *,
        expected_control_revision: str | None = None,
    ) -> AttachmentInfo:
        try:
            response = await self.client.post(
                self.base_url + paths.terminal_attachments(host_id, terminal_id),
                headers=await self._auth(),
                json={
                    "kind": kind,
                    "takeover": takeover,
                    "cols": cols,
                    "rows": rows,
                    **(
                        {"expected_control_revision": expected_control_revision}
                        if expected_control_revision is not None
                        else {}
                    ),
                },
                timeout=self.timeout,
            )
        except httpx.HTTPError as exc:
            raise ShellOpError("host_offline", str(exc)) from exc
        info = AttachmentInfo.from_wire(self._checked(response))
        mark_current("attach")
        return info

    async def control_holder(self, host_id: str, terminal_id: str) -> dict[str, Any]:
        try:
            response = await self.client.get(
                self.base_url + paths.terminal_control(host_id, terminal_id),
                headers=await self._auth(),
                timeout=self.timeout,
            )
        except httpx.HTTPError as exc:
            raise ShellOpError("host_offline", str(exc)) from exc
        return self._checked(response)

    async def acquire_lease(
        self,
        attachment_id: str,
        mode: str,
        *,
        expected_generation: int | None = None,
    ) -> LeaseInfo:
        try:
            response = await self.client.post(
                self.base_url + paths.attachment_leases(attachment_id),
                headers=await self._auth(),
                json={
                    "mode": mode,
                    **(
                        {"expected_generation": expected_generation}
                        if expected_generation is not None
                        else {}
                    ),
                },
                timeout=self.timeout,
            )
        except httpx.HTTPError as exc:
            raise ShellOpError("host_offline", str(exc)) from exc
        lease = LeaseInfo.from_wire(self._checked(response))
        mark_current("lease" if mode == "control" else "view_lease")
        return lease

    async def terminal_ticket(self, lease_id: str) -> str:
        try:
            response = await self.client.post(
                self.base_url + paths.TERMINAL_TICKETS,
                headers=await self._auth(),
                json={"lease_id": lease_id},
                timeout=self.timeout,
            )
        except httpx.HTTPError as exc:
            raise ShellOpError("host_offline", str(exc)) from exc
        doc = self._checked(response)
        ticket = doc.get("ticket")
        if not isinstance(ticket, str) or not ticket:
            raise ShellOpError("upstream_incompatible", "ticket missing from response")
        mark_current("ticket")
        return ticket

    async def release_lease(self, lease_id: str) -> None:
        try:
            response = await self.client.delete(
                self.base_url + paths.lease(lease_id),
                headers=await self._auth(),
                timeout=self.timeout,
            )
        except httpx.HTTPError as exc:
            raise ShellOpError("host_offline", str(exc)) from exc
        self._checked(response)

    async def close_attachment(self, attachment_id: str) -> None:
        try:
            response = await self.client.delete(
                self.base_url + paths.attachment(attachment_id),
                headers=await self._auth(),
                timeout=self.timeout,
            )
        except httpx.HTTPError as exc:
            raise ShellOpError("host_offline", str(exc)) from exc
        self._checked(response)

    async def close_terminal(self, command: dict[str, Any]) -> dict[str, Any]:
        try:
            response = await self.client.post(
                self.base_url + paths.host_commands(quote(command["host_id"], safe="")),
                headers=await self._auth(),
                json=command,
                timeout=self.timeout,
            )
        except httpx.HTTPError as exc:
            raise ShellOpError("host_offline", str(exc)) from exc
        return self._checked(response)


@dataclass
class _ScriptedAttachment:
    info: AttachmentInfo
    closed: bool = False


@dataclass
class ScriptedShellGatewayOps(ShellGatewayOps):
    """Deterministic ownership fake (no network). Models first attach,
    same-account conflict with owner disclosure, explicit takeover with a
    generation bump + epoch rotation, control/view leases, release (the
    attachment survives), and close (terminal: leases die, re-attach
    starts a fresh attachment id at generation 1, mirroring LeaseStore).

    `account`/`device_id` identify the caller; `seed_owner` pre-owns a
    terminal by someone else to force the conflict path.
    """

    account: str = "acct-1"
    device_id: str = "dev-1"
    label: str = "this device"
    attaches: list[dict[str, Any]] = field(default_factory=list)
    leases: list[dict[str, Any]] = field(default_factory=list)
    tickets: list[str] = field(default_factory=list)
    releases: list[str] = field(default_factory=list)
    closes: list[str] = field(default_factory=list)
    close_commands: list[dict[str, Any]] = field(default_factory=list)
    _attachments: dict[tuple[str, str], _ScriptedAttachment] = field(default_factory=dict)
    _by_id: dict[str, _ScriptedAttachment] = field(default_factory=dict)
    _leases: dict[str, LeaseInfo] = field(default_factory=dict)
    _live_leases: set[str] = field(default_factory=set)
    _control_holder: dict[str, tuple[str, str]] = field(default_factory=dict)
    _control_issued: dict[str, int] = field(default_factory=dict)
    _pending_control: set[str] = field(default_factory=set)
    _att_seq: int = 0
    _lease_seq: int = 0
    _ticket_seq: int = 0

    def seed_owner(
        self, host_id: str, terminal_id: str, device_id: str, label: str
    ) -> AttachmentInfo:
        """Pre-own a terminal by another holder (same account)."""
        self._att_seq += 1
        info = AttachmentInfo(
            attachment_id=f"att-{self._att_seq}",
            host_id=host_id,
            terminal_id=terminal_id,
            kind="shell",
            generation=1,
            epoch="e1",
            owner_device_id=device_id,
            owner_label=label,
            takeover_authorized=True,
        )
        record = _ScriptedAttachment(info)
        self._attachments[(host_id, terminal_id)] = record
        self._by_id[info.attachment_id] = record
        return info

    async def attach(
        self,
        host_id: str,
        terminal_id: str,
        kind: str,
        takeover: bool,
        cols: int = 80,
        rows: int = 24,
        *,
        expected_control_revision: str | None = None,
    ) -> AttachmentInfo:
        if expected_control_revision is not None:
            state = await self.control_holder(host_id, terminal_id)
            if expected_control_revision != state["control_revision"] or (
                state["held"] and not state["mine"]
            ):
                raise ShellOpError("lease_conflict", "control holder changed", state)
        self.attaches.append(
            {
                "host_id": host_id,
                "terminal_id": terminal_id,
                "kind": kind,
                "takeover": takeover,
            }
        )
        if kind == "agent" and takeover is not True:
            raise ShellOpError(
                "forbidden",
                "agent attach takes upstream ownership; explicit takeover required",
                details={"reason": "agent_requires_takeover"},
            )
        record = self._attachments.get((host_id, terminal_id))
        if record is not None and not record.closed:
            current = record.info
            if takeover is True:
                self._kill_attachment_leases(current.attachment_id)
                bumped = AttachmentInfo(
                    attachment_id=current.attachment_id,
                    host_id=current.host_id,
                    terminal_id=current.terminal_id,
                    kind=current.kind,
                    generation=current.generation + 1,
                    epoch=f"e{current.generation + 1}",
                    owner_device_id=self.device_id,
                    owner_label=self.label,
                    takeover_authorized=True,
                )
                record.info = bumped
                self._pending_control.add(bumped.attachment_id)
                return bumped
            raise ShellOpError(
                "lease_conflict",
                f"terminal owned by {current.owner_display}",
                details={
                    "owner": {
                        "device_id": current.owner_device_id,
                        "label": current.owner_label,
                    }
                },
            )
        self._att_seq += 1
        info = AttachmentInfo(
            attachment_id=f"att-{self._att_seq}",
            host_id=host_id,
            terminal_id=terminal_id,
            kind=kind,
            generation=1,
            epoch="e1",
            owner_device_id=self.device_id,
            owner_label=self.label,
            takeover_authorized=True,
        )
        record = _ScriptedAttachment(info)
        self._attachments[(host_id, terminal_id)] = record
        self._by_id[info.attachment_id] = record
        self._pending_control.add(info.attachment_id)
        return info

    async def control_holder(self, host_id: str, terminal_id: str) -> dict[str, Any]:
        record = self._attachments.get((host_id, terminal_id))
        info = record.info if record is not None else None
        holder = (
            self._control_holder.get(info.attachment_id)
            if info and record is not None and not record.closed
            else None
        )
        if (
            holder is None
            and info
            and record is not None
            and not record.closed
            and info.attachment_id in self._pending_control
        ):
            holder = (self.account, info.owner_device_id)
        stamp = (
            host_id,
            terminal_id,
            self.account,
            info,
            record.closed if record else None,
            self._control_issued.get(info.attachment_id, 0) if info else 0,
            holder,
        )
        state: dict[str, Any] = {
            "held": holder is not None,
            "mine": holder == (self.account, self.device_id),
            "control_revision": hashlib.sha256(repr(stamp).encode()).hexdigest(),
        }
        if holder and not state["mine"]:
            state["holder"] = {"device_id": holder[1]}
        return state

    async def acquire_lease(
        self,
        attachment_id: str,
        mode: str,
        *,
        expected_generation: int | None = None,
    ) -> LeaseInfo:
        self.leases.append({"attachment_id": attachment_id, "mode": mode})
        record = self._by_id.get(attachment_id)
        if record is None or record.closed:
            raise ShellOpError("lease_expired", "attachment closed")
        if expected_generation is not None and (
            record.info.generation != expected_generation
            or record.info.owner_device_id != self.device_id
        ):
            raise ShellOpError("lease_conflict", "attachment changed")
        if mode == "control":
            holder = self._control_holder.get(attachment_id)
            if holder is not None and holder != (self.account, self.device_id):
                raise ShellOpError(
                    "lease_conflict",
                    "control held by another device",
                    details={"holder": {"device_id": holder[1]}},
                )
            self._control_holder[attachment_id] = (self.account, self.device_id)
        self._lease_seq += 1
        info = record.info
        lease = LeaseInfo(
            lease_id=f"lease-{self._lease_seq}",
            attachment_id=attachment_id,
            host_id=info.host_id,
            terminal_id=info.terminal_id,
            mode=mode,
            generation=info.generation,
            epoch=info.epoch,
            expires_at="2999-01-01T00:00:00Z",
        )
        if mode == "control":
            self._control_issued[attachment_id] = self._lease_seq
            self._pending_control.discard(attachment_id)
        self._leases[lease.lease_id] = lease
        self._live_leases.add(lease.lease_id)
        return lease

    async def terminal_ticket(self, lease_id: str) -> str:
        self.tickets.append(lease_id)
        if lease_id not in self._live_leases:
            raise ShellOpError("lease_expired", "no such lease")
        self._ticket_seq += 1
        return f"scripted-ticket-{self._ticket_seq}"

    async def release_lease(self, lease_id: str) -> None:
        self.releases.append(lease_id)
        lease = self._leases.get(lease_id)
        if lease is None or lease_id not in self._live_leases:
            raise ShellOpError("not_found", "no such lease")
        self._live_leases.discard(lease_id)
        if self._control_holder.get(lease.attachment_id) == (self.account, self.device_id):
            del self._control_holder[lease.attachment_id]

    async def close_attachment(self, attachment_id: str) -> None:
        self.closes.append(attachment_id)
        record = self._by_id.get(attachment_id)
        if record is None or record.closed:
            raise ShellOpError("not_found", "no such attachment")
        record.closed = True
        self._kill_attachment_leases(attachment_id)

    async def close_terminal(self, command: dict[str, Any]) -> dict[str, Any]:
        """Record the session.close command; the terminal's attachment closes with it."""
        self.close_commands.append(command)
        record = self._attachments.get((command["host_id"], command["target"]["terminal_id"]))
        if record is not None and not record.closed:
            record.closed = True
            self._kill_attachment_leases(record.info.attachment_id)
        return {"command_id": command["command_id"], "status": "accepted"}

    def _kill_attachment_leases(self, attachment_id: str) -> None:
        for lease_id, lease in self._leases.items():
            if lease.attachment_id == attachment_id:
                self._live_leases.discard(lease_id)
        self._control_holder.pop(attachment_id, None)

    def is_live(self, lease_id: str) -> bool:
        return lease_id in self._live_leases

    def is_closed(self, attachment_id: str) -> bool:
        record = self._by_id.get(attachment_id)
        return record is not None and record.closed


__all__ = [
    "AttachmentInfo",
    "HttpxShellGatewayOps",
    "LeaseInfo",
    "ScriptedShellGatewayOps",
    "ShellGatewayOps",
    "ShellOpError",
    "owner_display_from_details",
]
