"""Identity: PKCE verification, single-use nonces, opaque sessions.

No JWT, no passwords. Tokens are opaque random strings; all state lives
server-side with injected clocks and bounded stores.
"""

from __future__ import annotations

import base64
import hashlib
import json
import logging
import os
import secrets
import tempfile
import threading
from collections.abc import Callable, Collection, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from relaydeck.persistence_contracts import KeyUnavailable, Record, StorageUnavailable, Table

if TYPE_CHECKING:
    from relaydeck.companion.persistence import PersistentStore, Transaction

try:
    import fcntl
except ImportError:  # pragma: no cover - non-POSIX fallback (no inter-process lock)
    fcntl = None  # type: ignore[assignment]

LOG = logging.getLogger("relaydeck.gateway.identity")

ACCESS_TTL_MS = 15 * 60 * 1000
REFRESH_TTL_MS = 30 * 24 * 60 * 60 * 1000
NONCE_TTL_MS = 10 * 60 * 1000
TICKET_TTL_MS = 10 * 1000
MAX_NONCES = 10_000
MAX_SESSIONS = 100_000
MAX_TICKETS = 10_000


def pkce_challenge(verifier: str) -> str:
    digest = hashlib.sha256(verifier.encode()).digest()
    return base64.urlsafe_b64encode(digest).decode().rstrip("=")


def pkce_verify(verifier: str, challenge: str) -> bool:
    if not verifier or not challenge:
        return False
    try:
        expect = pkce_challenge(verifier)
    except Exception:  # noqa: BLE001 - any encoding failure rejects
        return False
    return secrets.compare_digest(expect, challenge)


@dataclass
class _Nonce:
    scope: str
    expires_ms: int


@dataclass
class NonceStore:
    _nonces: dict[str, _Nonce] = field(default_factory=dict)

    def issue(self, scope: str, now_ms: int, ttl_ms: int = NONCE_TTL_MS) -> str:
        self._evict(now_ms)
        if len(self._nonces) >= MAX_NONCES:
            oldest = min(self._nonces, key=lambda k: self._nonces[k].expires_ms)
            del self._nonces[oldest]
        nonce = secrets.token_urlsafe(32)
        self._nonces[nonce] = _Nonce(scope, now_ms + ttl_ms)
        return nonce

    def claim(self, nonce: str, scope: str, now_ms: int) -> bool:
        entry = self._nonces.get(nonce)
        if entry is None:
            return False
        if entry.scope != scope or entry.expires_ms < now_ms:
            return False
        del self._nonces[nonce]
        return True

    def _evict(self, now_ms: int) -> None:
        expired = [k for k, v in self._nonces.items() if v.expires_ms < now_ms]
        for key in expired:
            del self._nonces[key]


@dataclass(frozen=True)
class Session:
    account: str
    capabilities: frozenset[str]
    hosts: frozenset[str]
    access_expires_ms: int
    refresh_expires_ms: int
    device_id: str = ""
    # Nonempty only for local device pairing: exact host enrollment binding.
    # These sessions never inherit other hosts owned by the local principal.
    host_binding: str = ""


@dataclass
class SessionStore:
    _by_access: dict[str, Session] = field(default_factory=dict, repr=False)
    _refresh_to_access: dict[str, str] = field(default_factory=dict, repr=False)
    persistence: PersistentStore | None = field(default=None, repr=False)
    origin: str = ""
    generation_reader: Callable[[], int] | None = field(default=None, repr=False)
    _generation: int = field(default=0, init=False)
    _lock: threading.RLock = field(default_factory=threading.RLock, init=False, repr=False)
    _denied: set[str] = field(default_factory=set, init=False, repr=False)
    _denied_scopes: set[tuple[str, ...]] = field(default_factory=set, init=False, repr=False)
    _known_pairs: dict[str, tuple[Session, str]] = field(
        default_factory=dict, init=False, repr=False
    )
    _pending_revocations: dict[str, Session] = field(default_factory=dict, init=False, repr=False)

    def __post_init__(self) -> None:
        if self.persistence is not None:
            if not self.origin or self.generation_reader is None:
                raise StorageUnavailable("invalid_record")
            self._generation = self.generation_reader()

    @staticmethod
    def verifier(token: str) -> str:
        return hashlib.sha256(token.encode()).hexdigest()

    def check_generation(self) -> bool:
        try:
            return (
                self.generation_reader is not None and self.generation_reader() == self._generation
            )
        except KeyUnavailable:
            raise StorageUnavailable("keys_unavailable") from None

    def _decode_authority(
        self, row: Record, *, revocation_only: bool = False
    ) -> tuple[Session, str, str] | None:
        if row.scope[0] != self.origin:
            return None  # A credential for another origin is not database corruption.
        try:
            doc = json.loads(row.body)
            if not revocation_only and doc["auth_generation"] != self._generation:
                return None
            session = Session(
                doc["account"],
                frozenset(doc["capabilities"]),
                frozenset(doc["hosts"]),
                doc["access_expires_ms"],
                doc["refresh_expires_ms"],
                doc["device_id"],
                doc["host_binding"],
            )
            if row.scope != (self.origin, session.account, session.device_id):
                raise ValueError
            return session, doc["access_hash"], doc["refresh_hash"]
        except (KeyError, TypeError, ValueError):
            raise StorageUnavailable("corrupt") from None

    def _persist_pair(
        self, tx: Transaction, pair: tuple[str, str], session: Session, now: int
    ) -> None:
        access_hash, refresh_hash = (self.verifier(token) for token in pair)
        body = json.dumps(
            {
                "account": session.account,
                "device_id": session.device_id,
                "capabilities": sorted(session.capabilities),
                "hosts": sorted(session.hosts),
                "host_binding": session.host_binding,
                "access_expires_ms": session.access_expires_ms,
                "refresh_expires_ms": session.refresh_expires_ms,
                "access_hash": access_hash,
                "refresh_hash": refresh_hash,
                "auth_generation": self._generation,
            },
            sort_keys=True,
        ).encode()
        for kind, hashed in (("access", access_hash), ("refresh", refresh_hash)):
            if not tx.put(
                Record(
                    Table.GATEWAY_VERIFIERS,
                    kind + ":" + hashed,
                    (self.origin, session.account, session.device_id),
                    body,
                    1,
                    now,
                    session.refresh_expires_ms,
                ),
                expected_revision=None,
            ):
                raise StorageUnavailable("revision_conflict")

    def _durable_session(self, token: str, kind: str) -> tuple[Session, str, str] | None:
        assert self.persistence is not None
        if not self.check_generation() or self.verifier(token) in self._denied:
            return None
        with self.persistence.transaction() as tx:
            row = tx.get(Table.GATEWAY_VERIFIERS, kind + ":" + self.verifier(token))
            result = self._decode_authority(row) if row else None
        if result and any(
            (self.origin, result[0].account, result[0].device_id)[: len(scope)] == scope
            for scope in self._denied_scopes
        ):
            return None
        return result

    def _rows(self, tx: Transaction, table: Table, scope: tuple[str, ...]) -> Iterator[Record]:
        cursor = None
        while True:
            page = tx.scan(table, scope=scope, after=cursor)
            yield from page.records
            if page.next_cursor is None:
                return
            cursor = page.next_cursor

    def _remove_pair(
        self,
        tx: Transaction,
        access_hash: str,
        refresh_hash: str,
        now: int,
        *,
        tombstone: bool = True,
    ) -> None:
        for kind, hashed in (("access", access_hash), ("refresh", refresh_hash)):
            key = kind + ":" + hashed
            old = tx.get(Table.GATEWAY_VERIFIERS, key)
            if old is not None:
                tx.delete(old.table, old.key, expected_revision=old.revision)
                if tombstone and tx.get(Table.REVOCATIONS, key) is None:
                    tx.put(
                        Record(Table.REVOCATIONS, key, old.scope, b"revoked", 1, now),
                        expected_revision=None,
                    )

    def _remove_web(self, tx: Transaction, scope: tuple[str, ...], hashes: set[str]) -> None:
        removed: set[str] = set()
        for row in self._rows(tx, Table.WEB_SESSIONS, scope):
            if json.loads(row.body)["access_hash"] in hashes:
                tx.delete(row.table, row.key, expected_revision=row.revision)
                removed.add(row.key)
        for row in self._rows(tx, Table.TAB_ALIASES, scope):
            if json.loads(row.body)["sid_hash"] in removed:
                tx.delete(row.table, row.key, expected_revision=row.revision)

    def prepare_refresh(
        self,
        tx: Transaction,
        refresh: str,
        now_ms: int,
        allowed: Callable[[Session], bool] | None = None,
    ) -> tuple[tuple[str, str], Session, str, str] | None:
        if not self.check_generation() or self.verifier(refresh) in self._denied:
            return None
        row = tx.get(Table.GATEWAY_VERIFIERS, "refresh:" + self.verifier(refresh))
        decoded = self._decode_authority(row) if row else None
        if decoded is None:
            return None
        old, access_hash, refresh_hash = decoded
        if any(
            (self.origin, old.account, old.device_id)[: len(scope)] == scope
            for scope in self._denied_scopes
        ):
            return None
        if old.refresh_expires_ms < now_ms or (allowed is not None and not allowed(old)):
            return None
        pair = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
        session = Session(
            old.account,
            old.capabilities,
            old.hosts,
            now_ms + ACCESS_TTL_MS,
            now_ms + REFRESH_TTL_MS,
            old.device_id,
            old.host_binding,
        )
        # Consumed refresh pairs are denied by deletion. Restored backups are
        # invalidated by the external generation, not unbounded tombstones.
        self._remove_pair(tx, access_hash, refresh_hash, now_ms, tombstone=False)
        self._persist_pair(tx, pair, session, now_ms)
        return pair, session, access_hash, refresh_hash

    def publish_refresh(self, result: tuple[tuple[str, str], Session, str, str]) -> None:
        pair, session, access_hash, refresh_hash = result
        self._known_pairs.pop(access_hash, None)
        for access in list(self._by_access):
            if self.verifier(access) == access_hash:
                self._by_access.pop(access, None)
        for refresh in list(self._refresh_to_access):
            if self.verifier(refresh) == refresh_hash:
                self._refresh_to_access.pop(refresh, None)
        self._by_access[pair[0]] = session
        self._refresh_to_access[pair[1]] = pair[0]
        self._known_pairs[self.verifier(pair[0])] = session, self.verifier(pair[1])

    def begin_revocation(self, access: str, *, refresh: str = "") -> Session | None:
        """Fence locally before I/O; this metadata is ONLY for revocation."""
        hashed = self.verifier(access)
        self._denied.add(hashed)
        known = self._known_pairs.get(hashed)
        if known is not None:
            self._pending_revocations[hashed] = known[0]
            self._denied.add(known[1])
        if refresh:
            self._denied.add(self.verifier(refresh))
        return self._pending_revocations.get(hashed) or self._by_access.get(access)

    def pending_revocation(self, access: str) -> Session | None:
        return self._pending_revocations.get(self.verifier(access))

    def revoke_in_transaction(
        self, tx: Transaction, access: str, *, scope: tuple[str, ...] | None = None
    ) -> Session | None:
        """Token-possession deletion only: never returns an access grant.

        Denied/expired credentials may retry. The encrypted row's origin and
        optional web identity scope still have to match before any deletion.
        """
        hashed = self.verifier(access)
        row = tx.get(Table.GATEWAY_VERIFIERS, "access:" + hashed)
        decoded = self._decode_authority(row, revocation_only=True) if row else None
        session = decoded[0] if decoded else self.pending_revocation(access)
        if session is not None:
            bound = (self.origin, session.account, session.device_id)
            if scope is not None and scope != bound:
                raise StorageUnavailable("invalid_record")
            self._denied.add(hashed)
            self._pending_revocations[hashed] = session
            if decoded is not None:
                if decoded[1] != hashed:
                    raise StorageUnavailable("corrupt")
                self._denied.add(decoded[2])
                assert row is not None
                self._remove_pair(tx, decoded[1], decoded[2], row.updated_ms)
            self._remove_web(tx, bound, {hashed})
        elif scope is not None:
            # Read absence is durable proof; a leftover matching web row may
            # still be deleted without recovering gateway authorization.
            self._remove_web(tx, scope, {hashed})
        return session

    def finish_revocation(self, access_hash: str) -> None:
        """Discard retry metadata ONLY after the transaction committed."""
        self._pending_revocations.pop(access_hash, None)
        self._known_pairs.pop(access_hash, None)
        for access in list(self._by_access):
            if self.verifier(access) == access_hash:
                self._by_access.pop(access, None)
        for refresh, access in list(self._refresh_to_access.items()):
            if self.verifier(access) == access_hash:
                self._refresh_to_access.pop(refresh, None)

    def _revoke_durable(
        self, scope: tuple[str, ...], predicate: Callable[[Session], bool]
    ) -> tuple[Session, ...]:
        assert self.persistence is not None
        self._denied_scopes.add(scope)
        with self._lock, self.persistence.transaction() as tx:
            dead: list[Session] = []
            hashes: set[str] = set()
            for row in self._rows(tx, Table.GATEWAY_VERIFIERS, scope):
                decoded = self._decode_authority(row)
                if (
                    not row.key.startswith("access:")
                    or decoded is None
                    or not predicate(decoded[0])
                ):
                    continue
                session, access_hash, refresh_hash = decoded
                self._remove_pair(tx, access_hash, refresh_hash, row.updated_ms)
                hashes.add(access_hash)
                dead.append(session)
            self._remove_web(tx, scope, hashes)
        self._denied.update(hashes)
        self._denied_scopes.discard(scope)
        for hashed in hashes:
            self.finish_revocation(hashed)
        for access in list(self._by_access):
            if self.verifier(access) in hashes:
                self._by_access.pop(access, None)
        for refresh, access in list(self._refresh_to_access.items()):
            if self.verifier(access) in hashes:
                self._refresh_to_access.pop(refresh, None)
        return tuple(dead)

    def _revoke_one(self, access: str) -> Session | None:
        assert self.persistence is not None
        with self._lock, self.persistence.transaction() as tx:
            session = self.revoke_in_transaction(tx, access)
        self.finish_revocation(self.verifier(access))
        if session is None:
            # Absence was durably read: invalid bearer attempts must not
            # accumulate a permanent in-memory denial history either.
            self._denied.discard(self.verifier(access))
        return session

    def _account_sessions(self, account: str) -> tuple[Session, ...]:
        if self.persistence is None:
            return tuple(self._by_access.values())
        if not self.check_generation():
            return ()
        with self.persistence.transaction() as tx:
            sessions = []
            for row in self._rows(tx, Table.GATEWAY_VERIFIERS, (self.origin, account)):
                decoded = self._decode_authority(row)
                if (
                    row.key.startswith("access:")
                    and decoded
                    and decoded[1] not in self._denied
                    and not any(row.scope[: len(scope)] == scope for scope in self._denied_scopes)
                ):
                    sessions.append(decoded[0])
        return tuple(sessions)

    def create(
        self,
        account: str,
        capabilities: Collection[str],
        hosts: Collection[str],
        now_ms: int,
        device_id: str = "",
        *,
        host_binding: str = "",
    ) -> tuple[str, str]:
        if isinstance(capabilities, str) or isinstance(hosts, str):
            raise ValueError("Session grants must be collections of names")
        if self.persistence is not None:
            pair = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
            session = Session(
                account,
                frozenset(capabilities),
                frozenset(hosts),
                now_ms + ACCESS_TTL_MS,
                now_ms + REFRESH_TTL_MS,
                device_id or f"dev_{secrets.token_urlsafe(16)}",
                host_binding,
            )
            with self._lock, self.persistence.transaction() as tx:
                if not self.check_generation():
                    raise StorageUnavailable("unavailable")
                self._persist_pair(tx, pair, session, now_ms)
            self._by_access[pair[0]], self._refresh_to_access[pair[1]] = session, pair[0]
            self._known_pairs[self.verifier(pair[0])] = session, self.verifier(pair[1])
            return pair
        if len(self._by_access) >= MAX_SESSIONS:
            self._evict(now_ms)
        access = secrets.token_urlsafe(32)
        refresh = secrets.token_urlsafe(32)
        self._by_access[access] = Session(
            account,
            frozenset(capabilities),
            frozenset(hosts),
            now_ms + ACCESS_TTL_MS,
            now_ms + REFRESH_TTL_MS,
            device_id or f"dev_{secrets.token_urlsafe(16)}",
            host_binding,
        )
        self._refresh_to_access[refresh] = access
        return access, refresh

    def lookup(self, access: str, now_ms: int) -> Session | None:
        if self.persistence is not None:
            result = self._durable_session(access, "access")
            if result and result[0].access_expires_ms >= now_ms:
                self._known_pairs[result[1]] = result[0], result[2]
                return result[0]
            return None
        session = self._by_access.get(access)
        if session is None or session.access_expires_ms < now_ms:
            return None
        return session

    def access_failure_reason(self, access: str, now_ms: int) -> str:
        decoded = self._durable_session(access, "access") if self.persistence is not None else None
        session = decoded[0] if decoded else self._by_access.get(access)
        if session is not None and session.access_expires_ms < now_ms <= session.refresh_expires_ms:
            return "access_expired"
        return "authority_changed"

    def refresh(
        self, refresh: str, now_ms: int, *, allowed: Callable[[Session], bool] | None = None
    ) -> tuple[str, str] | None:
        """Rotate: the presented refresh token is invalidated and a new
        pair is issued. Reuse of a rotated token fails (no reuse window)."""
        if self.persistence is not None:
            with self._lock, self.persistence.transaction() as tx:
                result = self.prepare_refresh(tx, refresh, now_ms, allowed)
                if result is not None:
                    self._remove_web(
                        tx, (self.origin, result[1].account, result[1].device_id), {result[2]}
                    )
            if result is not None:
                self.publish_refresh(result)
            return result[0] if result else None
        access = self._refresh_to_access.pop(refresh, None)
        if access is None:
            return None
        session = self._by_access.pop(access, None)
        if (
            session is None
            or session.refresh_expires_ms < now_ms
            or (allowed is not None and not allowed(session))
        ):
            return None
        return self.create(
            session.account,
            session.capabilities,
            session.hosts,
            now_ms,
            session.device_id,
            host_binding=session.host_binding,
        )

    def revoke_paired_host(self, host_id: str) -> tuple[Session, ...]:
        if self.persistence is not None:
            return self._revoke_durable(
                (self.origin,), lambda s: bool(s.host_binding) and host_id in s.hosts
            )
        doomed = [
            access
            for access, session in self._by_access.items()
            if session.host_binding and host_id in session.hosts
        ]
        return tuple(session for access in doomed if (session := self.revoke_session(access)))

    def revoke_session(self, access_token: str) -> Session | None:
        """Revoke one session (logout); returns the dead session if any."""
        if self.persistence is not None:
            self.begin_revocation(access_token)
            return self._revoke_one(access_token)
        session = self._by_access.pop(access_token, None)
        if session is None:
            return None
        stale = [r for r, a in self._refresh_to_access.items() if a == access_token]
        for refresh in stale:
            del self._refresh_to_access[refresh]
        return session

    def revoke_account(self, account: str) -> int:
        if self.persistence is not None:
            return len(self._revoke_durable((self.origin, account), lambda s: True))
        doomed = [a for a, s in self._by_access.items() if s.account == account]
        for access in doomed:
            del self._by_access[access]
        refreshes = [r for r, a in self._refresh_to_access.items() if a in set(doomed)]
        for refresh in refreshes:
            del self._refresh_to_access[refresh]
        return len(doomed)

    def revoke_device(self, account: str, device_id: str) -> int:
        if self.persistence is not None:
            return len(self._revoke_durable((self.origin, account, device_id), lambda s: True))
        doomed = [
            access
            for access, session in self._by_access.items()
            if session.account == account and session.device_id == device_id
        ]
        for access in doomed:
            self.revoke_session(access)
        return len(doomed)

    def device_metadata(self, account: str, now_ms: int) -> dict[str, dict[str, Any]]:
        rows: dict[str, dict[str, Any]] = {}
        for session in self._account_sessions(account):
            if session.account != account or session.refresh_expires_ms < now_ms:
                continue
            row = rows.setdefault(
                session.device_id,
                {
                    "device_id": session.device_id,
                    "auth_sessions": 0,
                    "access_expires_ms": 0,
                    "refresh_expires_ms": 0,
                },
            )
            row["auth_sessions"] += 1
            row["access_expires_ms"] = max(row["access_expires_ms"], session.access_expires_ms)
            row["refresh_expires_ms"] = max(row["refresh_expires_ms"], session.refresh_expires_ms)
        return rows

    def _evict(self, now_ms: int) -> None:
        expired = [a for a, s in self._by_access.items() if s.refresh_expires_ms < now_ms]
        for access in expired:
            del self._by_access[access]
        stale = [r for r, a in self._refresh_to_access.items() if a in set(expired)]
        for refresh in stale:
            del self._refresh_to_access[refresh]

    def notification_devices(
        self, account: str, host: str, now_ms: int, *, owned: bool = False
    ) -> frozenset[str]:
        return frozenset(
            session.device_id
            for session in self._account_sessions(account)
            if session.account == account
            and session.refresh_expires_ms > now_ms
            and "sessions.read" in session.capabilities
            and (host in session.hosts or (owned and not session.host_binding))
        )


@dataclass(frozen=True)
class TicketGrant:
    """Single-use terminal-socket grant.

    Short-lived, identity-bound (account, device, capabilities, hosts),
    and lease-bound. The ticket value is redacted by convention (rdt_
    prefix): it must never be logged. Capability checks at redeem time
    use this snapshot; live revocation is enforced separately by the
    socket registry, which severs the connection on revoke.
    """

    account: str
    device_id: str
    capabilities: frozenset[str]
    hosts: frozenset[str]
    lease_id: str
    host_id: str
    generation: int = 0
    resume: bool = False


@dataclass
class _Ticket:
    grant: TicketGrant
    expires_ms: int


@dataclass
class TicketStore:
    _tickets: dict[str, _Ticket] = field(default_factory=dict)

    def issue(self, grant: TicketGrant, now_ms: int) -> str:
        self._evict(now_ms)
        if len(self._tickets) >= MAX_TICKETS:
            oldest = min(self._tickets, key=lambda k: self._tickets[k].expires_ms)
            del self._tickets[oldest]
        ticket = f"rdt_{secrets.token_urlsafe(32)}"
        self._tickets[ticket] = _Ticket(grant, now_ms + TICKET_TTL_MS)
        return ticket

    def redeem(self, ticket: str, lease_id: str, now_ms: int) -> TicketGrant | None:
        entry = self._tickets.pop(ticket, None)
        if entry is None:
            return None
        if entry.expires_ms < now_ms or entry.grant.lease_id != lease_id:
            return None
        return entry.grant

    def has_resume(
        self, lease_id: str, account: str, device_id: str, generation: int, now_ms: int
    ) -> bool:
        self._evict(now_ms)
        return any(
            entry.grant.resume
            and entry.grant.lease_id == lease_id
            and entry.grant.account == account
            and entry.grant.device_id == device_id
            and entry.grant.generation == generation
            for entry in self._tickets.values()
        )

    def discard_resume(self, lease_id: str, account: str, device_id: str) -> None:
        """Successful replacement fences unused tickets for the preceding reader."""
        stale = [
            key
            for key, entry in self._tickets.items()
            if entry.grant.resume
            and entry.grant.lease_id == lease_id
            and entry.grant.account == account
            and entry.grant.device_id == device_id
        ]
        for key in stale:
            del self._tickets[key]

    def _evict(self, now_ms: int) -> None:
        expired = [k for k, v in self._tickets.items() if v.expires_ms < now_ms]
        for key in expired:
            del self._tickets[key]


ENROLLMENT_TTL_MS = 10 * 60 * 1000
MAX_ENROLLMENTS = 1_000
MAX_ENROLLMENT_ATTEMPTS = 5


@dataclass(frozen=True)
class Enrollment:
    """Owner-created invitation bound to account + connector pubkey."""

    id: str
    code: str
    account: str
    host_label: str
    pubkey: str
    fingerprint: str
    expires_ms: int


@dataclass
class _EnrollmentEntry:
    enrollment: Enrollment
    attempts: int = 0
    claimed: bool = False


ENROLLMENT_STORE_VERSION = 1


@contextmanager
def _exclusive(lock_path: str) -> Iterator[None]:
    """Hold an inter-process exclusive lock for a read-modify-write."""
    fd = os.open(lock_path, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        os.chmod(lock_path, 0o600)
        if fcntl is not None:
            fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        try:
            if fcntl is not None:
                fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)


@dataclass
class EnrollmentStore:
    """Single-use pubkey-bound invitations, optionally file-backed.

    With `path=None` this is a pure in-memory store. With `path` set,
    the file is the source of truth shared between the serving gateway
    (`serve --enrollment-store`) and the operator CLI (`issue-enrollment
    --store`): every create/attempt/claim runs under an inter-process
    lock, reloads the file first, and writes back atomically (0600).
    A missing file reads as empty; an unparseable file also reads as
    empty (fail closed: no redeem can succeed from it) and is logged.
    """

    _items: dict[str, _EnrollmentEntry] = field(default_factory=dict)
    path: str | None = None

    def create(
        self,
        account: str,
        host_label: str,
        pubkey: str,
        fingerprint: str,
        now_ms: int,
        ttl_ms: int = ENROLLMENT_TTL_MS,
    ) -> Enrollment:
        from relaydeck.connector.pubkey import mint_code

        if self.path is None:
            return self._create_memory(
                account, host_label, pubkey, fingerprint, now_ms, ttl_ms, mint_code
            )
        with _exclusive(self.path + ".lock"):
            self._load_file(now_ms)
            enrollment = self._create_memory(
                account, host_label, pubkey, fingerprint, now_ms, ttl_ms, mint_code
            )
            self._save_file()
            return enrollment

    def _create_memory(
        self,
        account: str,
        host_label: str,
        pubkey: str,
        fingerprint: str,
        now_ms: int,
        ttl_ms: int,
        mint_code: Any,
    ) -> Enrollment:
        self._evict(now_ms)
        if len(self._items) >= MAX_ENROLLMENTS:
            oldest = min(self._items, key=lambda k: self._items[k].enrollment.expires_ms)
            del self._items[oldest]
        enrollment = Enrollment(
            id=f"enr_{secrets.token_urlsafe(16)}",
            code=mint_code(),
            account=account,
            host_label=host_label,
            pubkey=pubkey,
            fingerprint=fingerprint,
            expires_ms=now_ms + ttl_ms,
        )
        self._items[enrollment.id] = _EnrollmentEntry(enrollment)
        return enrollment

    def attempt(self, enrollment_id: str, code: str, now_ms: int) -> Enrollment | None:
        """Validate id + code. Code guesses burn attempts (5 = burned).

        Returns the live enrollment without consuming it; the caller
        verifies the proof and then calls claim(). Unknown, expired,
        claimed, and burned invitations are indistinguishable (None).
        """
        if self.path is None:
            return self._attempt_memory(enrollment_id, code, now_ms)
        with _exclusive(self.path + ".lock"):
            self._load_file(now_ms)
            found = self._attempt_memory(enrollment_id, code, now_ms)
            self._save_file()
            return found

    def _attempt_memory(self, enrollment_id: str, code: str, now_ms: int) -> Enrollment | None:
        entry = self._items.get(enrollment_id)
        if entry is None or entry.claimed:
            return None
        enrollment = entry.enrollment
        if enrollment.expires_ms < now_ms or entry.attempts >= MAX_ENROLLMENT_ATTEMPTS:
            return None
        # Display grouping ("XXXX-XXXX-XXXX") and case are ignored.
        candidate = code.replace("-", "").upper()
        if not secrets.compare_digest(enrollment.code, candidate):
            entry.attempts += 1
            return None
        return enrollment

    def claim(self, enrollment_id: str) -> bool:
        if self.path is None:
            return self._claim_memory(enrollment_id)
        with _exclusive(self.path + ".lock"):
            self._load_file(None)
            claimed = self._claim_memory(enrollment_id)
            self._save_file()
            return claimed

    def status(self, enrollment_id: str, account: str, now_ms: int) -> str | None:
        if self.path is not None:
            with _exclusive(self.path + ".lock"):
                self._load_file(None)
                return self._status_memory(enrollment_id, account, now_ms)
        return self._status_memory(enrollment_id, account, now_ms)

    def _status_memory(self, enrollment_id: str, account: str, now_ms: int) -> str | None:
        entry = self._items.get(enrollment_id)
        if entry is None or entry.enrollment.account != account:
            return None
        if entry.claimed:
            return "claimed"
        if entry.enrollment.expires_ms <= now_ms:
            return "expired"
        return "burned" if entry.attempts >= MAX_ENROLLMENT_ATTEMPTS else "pending"

    def _claim_memory(self, enrollment_id: str) -> bool:
        entry = self._items.get(enrollment_id)
        if entry is None or entry.claimed:
            return False
        entry.claimed = True
        return True

    def _evict(self, now_ms: int) -> None:
        expired = [k for k, v in self._items.items() if v.enrollment.expires_ms < now_ms]
        for key in expired:
            del self._items[key]

    def _load_file(self, now_ms: int | None) -> None:
        """Reload shared state from the file (caller holds the lock)."""
        assert self.path is not None
        try:
            with open(self.path, encoding="utf-8") as handle:
                raw = json.load(handle)
        except FileNotFoundError:
            self._items = {}
            return
        except (OSError, ValueError) as exc:
            LOG.warning("enrollment store %s unreadable, treating as empty: %s", self.path, exc)
            self._items = {}
            return
        items: dict[str, _EnrollmentEntry] = {}
        if isinstance(raw, dict) and raw.get("version") == ENROLLMENT_STORE_VERSION:
            entries = raw.get("items")
            if isinstance(entries, dict):
                for key, value in entries.items():
                    entry = self._decode_entry(key, value)
                    if entry is not None:
                        items[key] = entry
        else:
            LOG.warning("enrollment store %s has unknown version, treating as empty", self.path)
        self._items = items
        if now_ms is not None:
            self._evict(now_ms)

    @staticmethod
    def _decode_entry(key: Any, value: Any) -> _EnrollmentEntry | None:
        if not isinstance(key, str) or not isinstance(value, dict):
            return None
        try:
            enrollment = Enrollment(
                id=key,
                code=str(value["code"]),
                account=str(value["account"]),
                host_label=str(value["host_label"]),
                pubkey=str(value["pubkey"]),
                fingerprint=str(value["fingerprint"]),
                expires_ms=int(value["expires_ms"]),
            )
            attempts = int(value.get("attempts", 0))
            claimed = bool(value.get("claimed", False))
        except (KeyError, TypeError, ValueError):
            return None
        if attempts < 0 or attempts > MAX_ENROLLMENT_ATTEMPTS:
            return None
        return _EnrollmentEntry(enrollment, attempts, claimed)

    def _save_file(self) -> None:
        """Write shared state atomically at 0600 (caller holds the lock)."""
        assert self.path is not None
        payload = {
            "version": ENROLLMENT_STORE_VERSION,
            "items": {
                key: {
                    "code": entry.enrollment.code,
                    "account": entry.enrollment.account,
                    "host_label": entry.enrollment.host_label,
                    "pubkey": entry.enrollment.pubkey,
                    "fingerprint": entry.enrollment.fingerprint,
                    "expires_ms": entry.enrollment.expires_ms,
                    "attempts": entry.attempts,
                    "claimed": entry.claimed,
                }
                for key, entry in self._items.items()
            },
        }
        directory = os.path.dirname(self.path) or "."
        os.makedirs(directory, mode=0o700, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=directory, prefix=".enrollments-", suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(payload, handle)
            os.chmod(tmp, 0o600)
            os.replace(tmp, self.path)
        except BaseException:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise
        os.chmod(self.path, 0o600)
