"""Terminal attachment ownership and downstream leases.

Upstream attachment (the connector's `herdr agent attach` / `herdr
terminal attach` PTY client) changes ownership and may resize the PTY:
it is never a silent side effect of viewing. The first attachment of a
terminal is an explicit authorization of that ownership. A shell attach
first attempts without takeover; when the terminal is already owned the
caller gets the current holder (same account only) and must take over
deliberately. Agent attach always takes ownership, so the contract
requires an explicit `takeover: true`.

Downstream, one attachment fans out to many read-only view leases but
at most one live control lease. A control lease binds principal,
device, terminal, upstream attachment generation and expiry (60s,
renewed while the client is foreground and authorized). This store
centralizes writer generations: any replacement (takeover, re-attach,
revocation) supersedes stale leases so old input callbacks fail with
stale_revision instead of driving the new writer.

Pure logic with an injected clock (`now_ms` on every call). No I/O, no
sockets: the gateway closes connections for the lease ids returned here.
"""
# Design notes:
# - Shell attach does not take over by default; agent attach takes ownership.
# - A refusal because another terminal is open names the holder (lease_conflict owner details,
#   same account only).
# - Replacing a session invalidates earlier callbacks, hence supersede-on-bump.
# - Leaving stops the local session while the remote pane survives; close is a separate
#   explicit action, hence release() vs close_attachment().

from __future__ import annotations

import hashlib
import secrets
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from typing import Any

from relaydeck.contracts.terminal_ownership import is_control_revision
from relaydeck.lease_diagnostics import LeaseTrace, opaque

CONTROL_LEASE_TTL_MS = 60_000
READER_HANDOFF_TTL_MS = 5_000
VIEW_LEASE_TTL_MS = 3_600_000
LEGACY_LEASE_TTL_MS = 3_600_000
MAX_LABEL_CHARS = 64

ATTACHMENT_KINDS = ("agent", "shell")
LEASE_MODES = ("view", "control")


class LeaseError(Exception):
    """Typed lease failure. `code` is a envelope code; `details`
    carries owner/holder disclosure already scrubbed for the requester
    (never cross-account identities)."""

    def __init__(
        self,
        code: str,
        message: str,
        retryable: bool = False,
        details: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.retryable = retryable
        self.details = dict(details or {})


@dataclass(frozen=True)
class Attachment:
    """One upstream-owned terminal stream. The id is stable across
    takeovers; the generation advances and the epoch rotates on every
    replacement so downstream viewers resync explicitly."""

    attachment_id: str
    host_id: str
    terminal_id: str
    kind: str
    generation: int
    epoch: str
    account: str
    device_id: str
    owner_label: str
    takeover_authorized: bool
    created_ms: int
    closed: bool = False


@dataclass
class LeaseRecord:
    """One downstream view/control lease. Mutable: expiry extends on
    renew, input_seq tracks the per-generation high-water mark."""

    lease_id: str
    attachment_id: str
    host_id: str
    terminal_id: str
    account: str
    device_id: str
    mode: str
    generation: int
    epoch: str
    expires_ms: int
    created_ms: int
    input_seq: int = 0
    live: bool = True
    superseded: bool = False
    handoff_original_expiry_ms: int | None = None


def _epoch(generation: int) -> str:
    return f"e{generation}"


def _sanitize_label(value: str) -> str:
    text = "".join(ch for ch in value.strip() if ch.isprintable())
    return text.strip()[:MAX_LABEL_CHARS]


@dataclass
class LeaseStore:
    """In-memory attachment/lease registry. Deterministic ids
    (att-N/lease-N) for tests; the gateway treats them as opaque."""

    _attachments: dict[str, Attachment] = field(default_factory=dict)
    _by_terminal: dict[tuple[str, str], str] = field(default_factory=dict)
    _leases: dict[str, LeaseRecord] = field(default_factory=dict)
    _control_by_attachment: dict[str, str] = field(default_factory=dict)
    _pending_control: dict[str, int] = field(default_factory=dict)
    _control_issued: dict[str, int] = field(default_factory=dict)
    _control_nonce: str = field(default_factory=lambda: secrets.token_hex(16), repr=False)
    _att_seq: int = 0
    _lease_seq: int = 0

    @staticmethod
    def _lease_trace(rec: LeaseRecord) -> LeaseTrace:
        return LeaseTrace(
            opaque("principal", rec.account, rec.device_id),
            opaque("terminal", rec.host_id, rec.terminal_id),
            rec.lease_id,
            rec.mode,
            rec.generation,
        )

    # -- lookup ------------------------------------------------------

    def get_attachment(self, attachment_id: str) -> Attachment | None:
        return self._attachments.get(attachment_id)

    def attachments_for_host(self, host_id: str, account: str) -> tuple[Attachment, ...]:
        return tuple(
            att
            for att in self._attachments.values()
            if att.host_id == host_id and att.account == account and not att.closed
        )

    def rotate_host(self, host_id: str) -> tuple[tuple[Attachment, tuple[str, ...]], ...]:
        """An upstream continuity loss invalidates every old stream/writer."""
        changed = []
        for att in tuple(self._attachments.values()):
            if att.host_id != host_id or att.closed:
                continue
            changed.append(self.rotate_attachment(att.attachment_id))
        return tuple(changed)

    def rotate_attachment(self, attachment_id: str) -> tuple[Attachment, tuple[str, ...]]:
        att = self._attachments[attachment_id]
        dead = self._supersede(attachment_id)
        renewed = replace(att, generation=att.generation + 1, epoch=_epoch(att.generation + 1))
        self._attachments[attachment_id] = renewed
        return renewed, dead

    def leases_of(self, attachment_id: str) -> tuple[LeaseRecord, ...]:
        return tuple(r for r in self._leases.values() if r.attachment_id == attachment_id)

    def upstream_mode(self, attachment_id: str, now_ms: int) -> str:
        """An observer must never retain Herdr's direct-attach size lock.

        Explicit attach has a bounded grace period to acquire its first lease.
        Afterwards only a live control lease keeps the writable attach client.
        """
        held = self._leases.get(self._control_by_attachment.get(attachment_id, ""))
        if held is not None and held.live and not held.superseded and held.expires_ms >= now_ms:
            return "control"
        return "control" if self._pending_control.get(attachment_id, -1) >= now_ms else "observe"

    def open_lease(self, lease_id: str, now_ms: int) -> LeaseRecord | None:
        """Live lease or None. Superseded, released, revoked and expired
        leases are not openable: ticket minting and socket accept fail
        with lease_expired."""
        rec = self._leases.get(lease_id)
        if rec is None or not rec.live or rec.superseded:
            return None
        if rec.expires_ms < now_ms:
            rec.live = False
            return None
        return rec

    def lease_host(self, lease_id: str) -> str | None:
        """Host of a lease regardless of liveness (route guard only).
        Unlike open_lease this never mutates, so the host-membership
        check runs before any liveness failure is reported."""
        rec = self._leases.get(lease_id)
        return rec.host_id if rec is not None else None

    def begin_reader_handoff(
        self, lease_id: str, account: str, device_id: str, generation: int, now_ms: int
    ) -> bool:
        """Preserve only the existing holder during a fenced reader replacement.

        The grace cannot extend the original lease or be prolonged by renewal,
        repeated resets or invalid resume cursors. There is no writer while
        the old reader is gone; explicit release/revocation still kills it.
        """
        rec = self.open_lease(lease_id, now_ms)
        if (
            rec is None
            or rec.account != account
            or rec.device_id != device_id
            or rec.generation != generation
            or rec.mode != "control"
        ):
            return False
        if rec.handoff_original_expiry_ms is None:
            rec.handoff_original_expiry_ms = rec.expires_ms
            rec.expires_ms = min(rec.expires_ms, now_ms + READER_HANDOFF_TTL_MS)
        return True

    def finish_reader_handoff(
        self, lease_id: str, account: str, device_id: str, generation: int, now_ms: int
    ) -> LeaseRecord:
        """Complete a validated cursor resume without acquiring control or resizing."""
        rec = self.open_lease(lease_id, now_ms)
        if rec is None:
            raise LeaseError("lease_expired", "reader handoff ended")
        if rec.account != account or rec.device_id != device_id:
            raise LeaseError("not_found", "no such lease")
        if rec.generation != generation or rec.handoff_original_expiry_ms is None:
            raise LeaseError("stale_revision", "reader handoff replaced")
        rec.expires_ms = rec.handoff_original_expiry_ms
        rec.handoff_original_expiry_ms = None
        return rec

    # -- upstream attachment ------------------------------------------

    def attach(
        self,
        *,
        host_id: str,
        terminal_id: str,
        kind: str,
        account: str,
        device_id: str,
        takeover: bool,
        owner_label: str,
        now_ms: int,
        expected_control_revision: str | None = None,
    ) -> tuple[Attachment, tuple[str, ...]]:
        """Authorize (first attach) or take over upstream ownership.

        Returns the attachment plus the ids of downstream leases
        displaced by a takeover (their sockets must be closed).
        Raises LeaseError lease_conflict with holder disclosure when
        owned by someone else and takeover is not set.
        """
        if kind not in ATTACHMENT_KINDS:
            raise LeaseError("forbidden", f"unknown attach kind {kind}")
        if takeover is not True and kind == "agent":
            raise LeaseError(
                "forbidden",
                "agent attach takes upstream ownership; explicit takeover required",
                details={"reason": "agent_requires_takeover"},
            )
        label = _sanitize_label(owner_label) or _sanitize_label(device_id)
        current = self._current(host_id, terminal_id)
        if current is not None and not current.closed and current.account != account:
            # A failed comparison must not disclose cross-account ownership either.
            raise LeaseError("not_found", "no such terminal")
        if expected_control_revision is not None:
            if not is_control_revision(expected_control_revision):
                raise LeaseError("forbidden", "malformed control revision")
            observed = self.control_holder(host_id, terminal_id, account, device_id, now_ms)
            if expected_control_revision != observed["control_revision"] or (
                observed["held"] and not observed["mine"]
            ):
                raise LeaseError(
                    "lease_conflict",
                    "control holder changed",
                    details={
                        "reason": "control_changed",
                        **({"holder": observed["holder"]} if "holder" in observed else {}),
                    },
                )
        if current is not None and not current.closed:
            if takeover is not True:
                raise LeaseError(
                    "lease_conflict",
                    self._owned_message(current, account),
                    details=self._owner_details(current, account),
                )
            dead = self._supersede(current.attachment_id)
            # Kind is sticky across takeovers: ownership (account, device,
            # label) transfers but the stream kind stays as first attached.
            attachment = replace(
                current,
                generation=current.generation + 1,
                epoch=_epoch(current.generation + 1),
                account=account,
                device_id=device_id,
                owner_label=label,
                takeover_authorized=True,
            )
            self._attachments[attachment.attachment_id] = attachment
            self._pending_control[attachment.attachment_id] = now_ms + CONTROL_LEASE_TTL_MS
            return attachment, dead
        self._att_seq += 1
        # Closing and reopening the same terminal must not reuse its old
        # stream epoch: delayed old bytes cannot belong to this attachment.
        generation = current.generation + 1 if current is not None else 1
        attachment = Attachment(
            attachment_id=f"att-{self._att_seq}",
            host_id=host_id,
            terminal_id=terminal_id,
            kind=kind,
            generation=generation,
            epoch=_epoch(generation),
            account=account,
            device_id=device_id,
            owner_label=label,
            takeover_authorized=True,
            created_ms=now_ms,
        )
        self._attachments[attachment.attachment_id] = attachment
        self._by_terminal[(host_id, terminal_id)] = attachment.attachment_id
        self._pending_control[attachment.attachment_id] = now_ms + CONTROL_LEASE_TTL_MS
        return attachment, ()

    def _current(self, host_id: str, terminal_id: str) -> Attachment | None:
        att_id = self._by_terminal.get((host_id, terminal_id))
        if att_id is None:
            return None
        return self._attachments.get(att_id)

    def _supersede(self, attachment_id: str) -> tuple[str, ...]:
        """Invalidate every downstream lease of an attachment after a
        generation bump. Superseded leases stay recorded so their input
        fails as stale_revision (writer replaced), not lease_expired."""
        dead: list[str] = []
        for rec in self._leases.values():
            if rec.attachment_id != attachment_id or rec.superseded or not rec.live:
                continue
            rec.superseded = True
            dead.append(rec.lease_id)
        self._control_by_attachment.pop(attachment_id, None)
        self._pending_control.pop(attachment_id, None)
        return tuple(dead)

    # -- downstream leases --------------------------------------------

    def acquire_control(
        self,
        attachment_id: str,
        account: str,
        device_id: str,
        now_ms: int,
        *,
        expected_generation: int | None = None,
    ) -> LeaseRecord:
        """Exclusive control writer. Re-acquire by the current holder
        renews (extends 60s); any other live holder conflicts. Expired
        holders are replaced, never resurrected."""
        attachment = self._open_attachment(attachment_id, account)
        if expected_generation is not None and (
            attachment.generation != expected_generation or attachment.device_id != device_id
        ):
            raise LeaseError(
                "lease_conflict",
                "attachment changed",
                details={"reason": "control_changed", **self._owner_details(attachment, account)},
            )
        held_id = self._control_by_attachment.get(attachment_id)
        held = self._leases.get(held_id) if held_id is not None else None
        if held is not None and held.live and not held.superseded:
            if held.expires_ms >= now_ms:
                if held.account == account and held.device_id == device_id:
                    held.handoff_original_expiry_ms = None
                    held.expires_ms = now_ms + CONTROL_LEASE_TTL_MS
                    self._lease_trace(held).emit("lease_issue", reason="reused", source="server")
                    return held
                raise LeaseError(
                    "lease_conflict",
                    self._held_message(held, account),
                    details=self._holder_details(held, account),
                )
            held.live = False
        self._control_by_attachment.pop(attachment_id, None)
        rec = self._issue(attachment, account, device_id, "control", CONTROL_LEASE_TTL_MS, now_ms)
        self._control_by_attachment[attachment_id] = rec.lease_id
        self._pending_control.pop(attachment_id, None)
        return rec

    def control_holder(
        self, host_id: str, terminal_id: str, account: str, device_id: str, now_ms: int
    ) -> dict[str, Any]:
        """Read-only: is a live control lease held on this terminal, and is it
        the caller's? Holder details follow the same-account disclosure rule.
        Never issues, renews or supersedes anything."""
        att = self._current(host_id, terminal_id)
        if att is not None and att.account != account:
            att = None
        held = (
            self._leases.get(self._control_by_attachment.get(att.attachment_id, ""))
            if att
            else None
        )
        if held is not None and (
            not held.live
            or held.superseded
            or held.expires_ms < now_ms
            or att is None
            or att.closed
        ):
            held = None
        pending = bool(
            att
            and not att.closed
            and held is None
            and self._pending_control.get(att.attachment_id, -1) >= now_ms
        )
        # Retain the last control issue across release/removal, so free -> held -> free is
        # a new observation too. Renewals and unrelated terminals do not invalidate it.
        stamp = (
            self._control_nonce,
            host_id,
            terminal_id,
            account,
            (att.attachment_id, att.generation, att.closed) if att else None,
            self._control_issued.get(att.attachment_id, 0) if att else 0,
            held.lease_id if held else None,
            pending,
        )
        out: dict[str, Any] = {
            "held": held is not None or pending,
            "mine": False,
            "control_revision": hashlib.sha256(repr(stamp).encode()).hexdigest(),
        }
        holder = held.device_id if held else att.device_id if pending and att is not None else None
        if holder is not None:
            out["mine"] = holder == device_id
            if not out["mine"]:
                out["holder"] = {"device_id": holder}
        return out

    def issue_view(
        self, attachment_id: str, account: str, device_id: str, now_ms: int
    ) -> LeaseRecord:
        """Read-only observer of an already-owned stream. Viewing never
        authorizes upstream ownership (the attachment must exist) and
        never installs a writer."""
        attachment = self._open_attachment(attachment_id, account)
        return self._issue(attachment, account, device_id, "view", VIEW_LEASE_TTL_MS, now_ms)

    def renew(self, lease_id: str, account: str, device_id: str, now_ms: int) -> LeaseRecord:
        """Extend a live holder's lease (foreground renewal). Holder
        mismatch and unknown ids are indistinguishable (not_found)."""
        rec = self._leases.get(lease_id)
        trace = (
            self._lease_trace(rec)
            if rec is not None
            else LeaseTrace(opaque("principal", account, device_id), "", lease_id)
        )
        if rec is None or not rec.live:
            trace.emit("renew_failure", reason="lease_expired", source="server_refusal")
            raise LeaseError("lease_expired", "lease gone")
        if rec.account != account or rec.device_id != device_id:
            trace.emit("renew_failure", reason="not_found", source="server_refusal")
            raise LeaseError("not_found", "no such lease")
        if rec.superseded:
            trace.emit("renew_failure", reason="stale_revision", source="server_refusal")
            raise LeaseError("stale_revision", "writer replaced", True)
        if rec.expires_ms < now_ms:
            rec.live = False
            trace.emit("renew_failure", reason="lease_expired", source="server_refusal")
            raise LeaseError("lease_expired", "lease expired")
        if rec.handoff_original_expiry_ms is not None:
            return rec
        ttl = CONTROL_LEASE_TTL_MS if rec.mode == "control" else VIEW_LEASE_TTL_MS
        rec.expires_ms = now_ms + ttl
        self._lease_trace(rec).emit("lease_renew", reason="ok", source="server")
        return rec

    def release(self, lease_id: str, account: str, device_id: str) -> Attachment:
        """Detach one lease. The attachment (and its remote process)
        survives; only this downstream view/control ends. Holder-only:
        like renew, a device mismatch is not_found, so a second device
        cannot release-then-acquire another device's control (no silent
        steal); forced displacement goes through attachment takeover."""
        rec = self._leases.get(lease_id)
        if rec is None or rec.account != account or rec.device_id != device_id:
            raise LeaseError("not_found", "no such lease")
        rec.live = False
        self._lease_trace(rec).emit("lease_release", reason="released", source="server")
        held_id = self._control_by_attachment.get(rec.attachment_id)
        if held_id == lease_id:
            del self._control_by_attachment[rec.attachment_id]
        attachment = self._attachments.get(rec.attachment_id)
        if attachment is None:  # pragma: no cover - store invariant
            raise LeaseError("not_found", "no such lease")
        return attachment

    def close_attachment(self, attachment_id: str, account: str) -> tuple[str, ...]:
        """Explicit close of the upstream attachment. All downstream
        leases die as expired (the stream is over, not replaced)."""
        attachment = self._attachments.get(attachment_id)
        if attachment is None or attachment.account != account:
            raise LeaseError("not_found", "no such attachment")
        self._attachments[attachment_id] = replace(attachment, closed=True)
        dead: list[str] = []
        for rec in self._leases.values():
            if rec.attachment_id == attachment_id and rec.live:
                rec.live = False
                dead.append(rec.lease_id)
        self._control_by_attachment.pop(attachment_id, None)
        return tuple(dead)

    # -- input path ----------------------------------------------------

    def check_input(self, lease_id: str, generation: int, input_seq: int, now_ms: int) -> bool:
        """Validate one input frame. True when new (record it), False
        when the sequence is a duplicate within this generation
        (deduplicated, not an error). View leases have no writer:
        any input on them conflicts."""
        rec = self._leases.get(lease_id)
        if rec is None or not rec.live:
            raise LeaseError("lease_expired", "lease gone")
        if rec.expires_ms < now_ms:
            rec.live = False
            raise LeaseError("lease_expired", "lease expired")
        if rec.superseded or generation != rec.generation:
            raise LeaseError("stale_revision", "stale writer generation", True)
        if rec.mode != "control":
            raise LeaseError("lease_conflict", "view lease has no writer")
        if rec.handoff_original_expiry_ms is not None:
            raise LeaseError("lease_conflict", "reader handoff has no active writer")
        if input_seq <= rec.input_seq:
            return False
        rec.input_seq = input_seq
        return True

    # -- revocation ----------------------------------------------------
    # NOTE (gap, not coverage): pause-policy writer invalidation
    # (backgrounding invalidates control writers) is not done here
    # (Android lifecycle); only the explicit revoke_* paths exist here.

    def revoke_account(self, account: str) -> tuple[str, ...]:
        return self._revoke(lambda r: r.account == account)

    def revoke_device(self, account: str, device_id: str) -> tuple[str, ...]:
        return self._revoke(lambda r: r.account == account and r.device_id == device_id)

    def revoke_host(self, host_id: str) -> tuple[str, ...]:
        return self._revoke(lambda r: r.host_id == host_id)

    def _revoke(self, match: Callable[[LeaseRecord], bool]) -> tuple[str, ...]:
        dead: list[str] = []
        for rec in self._leases.values():
            if rec.live and match(rec):
                rec.live = False
                dead.append(rec.lease_id)
        for att_id, held_id in list(self._control_by_attachment.items()):
            held = self._leases.get(held_id)
            if held is None or not held.live:
                del self._control_by_attachment[att_id]
        return tuple(dead)

    # -- legacy test/dev seam -------------------------------------------

    def legacy_issue(
        self,
        host_id: str,
        terminal_id: str,
        owner: str,
        generation: int,
        epoch: str,
        now_ms: int,
    ) -> LeaseRecord:
        """Legacy issue path: get-or-create the attachment without
        an ownership check and pin a replacement control lease at the
        requested generation. Tests and development only; production
        uses attach + acquire_control/issue_view."""
        current = self._current(host_id, terminal_id)
        if current is None or current.closed:
            self._att_seq += 1
            current = Attachment(
                attachment_id=f"att-{self._att_seq}",
                host_id=host_id,
                terminal_id=terminal_id,
                kind="shell",
                generation=generation,
                epoch=epoch,
                account=owner,
                device_id="",
                owner_label=_sanitize_label(owner),
                takeover_authorized=True,
                created_ms=now_ms,
            )
            self._attachments[current.attachment_id] = current
            self._by_terminal[(host_id, terminal_id)] = current.attachment_id
        else:
            self._supersede(current.attachment_id)
        self._lease_seq += 1
        rec = LeaseRecord(
            lease_id=f"lease-{self._lease_seq}",
            attachment_id=current.attachment_id,
            host_id=host_id,
            terminal_id=terminal_id,
            account=owner,
            device_id="",
            mode="control",
            generation=generation,
            epoch=epoch,
            expires_ms=now_ms + LEGACY_LEASE_TTL_MS,
            created_ms=now_ms,
        )
        self._leases[rec.lease_id] = rec
        self._lease_trace(rec).emit("lease_issue", reason="ok", source="server")
        self._control_by_attachment[current.attachment_id] = rec.lease_id
        self._control_issued[current.attachment_id] = self._lease_seq
        return rec

    def hard_remove(self, lease_id: str) -> None:
        rec = self._leases.pop(lease_id, None)
        if rec is not None and self._control_by_attachment.get(rec.attachment_id) == lease_id:
            del self._control_by_attachment[rec.attachment_id]

    # -- internals -------------------------------------------------------

    def _open_attachment(self, attachment_id: str, account: str) -> Attachment:
        attachment = self._attachments.get(attachment_id)
        if attachment is None:
            raise LeaseError("not_found", "no such attachment")
        if attachment.account != account:
            # Host membership is enforced by the route; account mismatch
            # here stays indistinguishable from unknown.
            raise LeaseError("not_found", "no such attachment")
        if attachment.closed:
            raise LeaseError("lease_expired", "attachment closed")
        return attachment

    def _issue(
        self,
        attachment: Attachment,
        account: str,
        device_id: str,
        mode: str,
        ttl_ms: int,
        now_ms: int,
    ) -> LeaseRecord:
        self._lease_seq += 1
        rec = LeaseRecord(
            lease_id=f"lease-{self._lease_seq}",
            attachment_id=attachment.attachment_id,
            host_id=attachment.host_id,
            terminal_id=attachment.terminal_id,
            account=account,
            device_id=device_id,
            mode=mode,
            generation=attachment.generation,
            epoch=attachment.epoch,
            expires_ms=now_ms + ttl_ms,
            created_ms=now_ms,
        )
        self._leases[rec.lease_id] = rec
        if mode == "control":
            self._control_issued[attachment.attachment_id] = self._lease_seq
        self._lease_trace(rec).emit("lease_issue", reason="ok", source="server")
        return rec

    def _owner_details(self, attachment: Attachment, requester: str) -> dict[str, Any]:
        if attachment.account != requester:
            return {}
        return {
            "owner": {
                "device_id": attachment.device_id,
                "label": attachment.owner_label,
            }
        }

    def _owned_message(self, attachment: Attachment, requester: str) -> str:
        if attachment.account != requester:
            return "terminal owned by another session"
        who = attachment.owner_label or attachment.device_id or "another device"
        return f"terminal owned by {who}"

    def _holder_details(self, rec: LeaseRecord, requester: str) -> dict[str, Any]:
        if rec.account != requester:
            return {}
        return {"holder": {"device_id": rec.device_id}}

    def _held_message(self, rec: LeaseRecord, requester: str) -> str:
        if rec.account != requester:
            return "control held by another session"
        who = rec.device_id or "another device"
        return f"control held by {who}"
