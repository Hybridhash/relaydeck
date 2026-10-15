"""Connector host registry and tokens.

Hosts authenticate with long-lived opaque bearer tokens issued through a
single-use enrollment nonce. One active token per host: re-enrollment
rotates (the old token dies immediately). Revocation is explicit and
auditable via return counts.

Operator revoke channel: the CLI cannot reach serving-process memory,
so `revoke-host --spool` writes a request file and the serving process
drains the spool directory through its lifespan consumer
and revokes each listed host. Revoking one host never touches the
others. The optional private file stores token hashes and preserves rotation
and revocation across a serving restart; in-memory mode remains available for
tests. Sessions, leases and byte streams are never restored by this registry.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import secrets
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path

from relaydeck.companion.private import private_dir, read_private, write_json

LOG = logging.getLogger("relaydeck.gateway.hosts")

REVOKE_SPOOL_VERSION = 1


@dataclass(frozen=True)
class HostRecord:
    host_id: str
    label: str
    token_prefix: str
    account: str = ""
    pubkey: str = ""
    fingerprint: str = ""


@dataclass
class HostStore:
    _by_token: dict[str, HostRecord] = field(default_factory=dict)
    _token_by_host: dict[str, str] = field(default_factory=dict)
    path: Path | None = None
    _revoked: dict[str, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.path is None:
            return
        private_dir(self.path.parent)
        if not self.path.exists() and not self.path.is_symlink():
            return
        body = json.loads(read_private(self.path))
        if not isinstance(body, dict) or body.get("version") not in (1, 2):
            raise ValueError("invalid durable host registry")
        revoked = body.get("revoked", {})
        if (
            not isinstance(revoked, dict)
            or len(revoked) > 1000
            or any(
                not isinstance(k, str) or not k or not isinstance(v, str)
                for k, v in revoked.items()
            )
        ):
            raise ValueError("invalid durable host revocations")
        self._revoked = revoked
        rows = body.get("hosts")
        if not isinstance(rows, list) or len(rows) > 1000:
            raise ValueError("invalid durable host registry")
        for row in rows:
            if (
                not isinstance(row, dict)
                or set(row)
                != {
                    "token_hash",
                    "host_id",
                    "label",
                    "token_prefix",
                    "account",
                    "pubkey",
                    "fingerprint",
                }
                or any(not isinstance(v, str) for v in row.values())
            ):
                raise ValueError("invalid durable host record")
            token_hash = row["token_hash"]
            if (
                len(token_hash) != 64
                or any(c not in "0123456789abcdef" for c in token_hash)
                or not row["host_id"]
                or row["host_id"] in self._token_by_host
                or row["host_id"] in self._revoked
                or token_hash in self._by_token
            ):
                raise ValueError("invalid durable host binding")
            record = HostRecord(**{k: v for k, v in row.items() if k != "token_hash"})
            self._by_token[token_hash] = record
            self._token_by_host[record.host_id] = token_hash

    def _key(self, token: str) -> str:
        return hashlib.sha256(token.encode()).hexdigest() if self.path else token

    def _commit(
        self, records: dict[str, HostRecord], revoked: dict[str, str] | None = None
    ) -> None:
        revoked = self._revoked if revoked is None else revoked
        if self.path is not None:
            from dataclasses import asdict

            write_json(
                self.path,
                {
                    "version": 2,
                    "revoked": revoked,
                    "hosts": [
                        {"token_hash": key, **asdict(value)} for key, value in records.items()
                    ],
                },
            )
        self._by_token = records
        self._token_by_host = {record.host_id: key for key, record in records.items()}
        self._revoked = revoked

    @staticmethod
    def _mint() -> tuple[str, str]:
        token = f"rdh_{secrets.token_urlsafe(32)}"
        return token, token[:12]

    def enroll(
        self,
        host_id: str,
        label: str = "",
        account: str = "",
        pubkey: str = "",
        fingerprint: str = "",
    ) -> str:
        """Issue (or rotate) the host token. Old token dies at once."""
        records = dict(self._by_token)
        old = self._token_by_host.get(host_id)
        if old is not None:
            records.pop(old, None)
        token, prefix = self._mint()
        records[self._key(token)] = HostRecord(
            host_id, label or host_id, prefix, account, pubkey, fingerprint
        )
        revoked = dict(self._revoked)
        revoked.pop(host_id, None)
        self._commit(records, revoked)
        return token

    def lookup(self, token: str) -> HostRecord | None:
        if not token:
            return None
        key = self._key(token)
        record = self._by_token.get(key)
        if record is None:
            return None
        # Token must be the host's current one (rotation-safe double check).
        if self._token_by_host.get(record.host_id) != key:
            return None
        return record

    def revoke(self, host_id: str) -> bool:
        token = self._token_by_host.get(host_id)
        if token is None:
            return False
        records = dict(self._by_token)
        revoked = dict(self._revoked)
        revoked[host_id] = records[token].account
        records.pop(token, None)
        self._commit(records, revoked)
        return True

    def hosts(self) -> tuple[HostRecord, ...]:
        return tuple(self._by_token[token] for token in self._token_by_host.values())

    def record_for_host(self, host_id: str) -> HostRecord | None:
        token = self._token_by_host.get(host_id)
        if token is None:
            return None
        return self._by_token.get(token)

    def allowed(self, account: str, host_id: str) -> bool:
        if host_id in self._revoked:
            return False
        record = self.record_for_host(host_id)
        return record is None or not record.account or record.account == account

    def owner(self, host_id: str) -> str | None:
        record = self.record_for_host(host_id)
        return record.account if record is not None else self._revoked.get(host_id)

    def owned_ids(self, account: str) -> frozenset[str]:
        return frozenset(record.host_id for record in self.hosts() if record.account == account)


def write_revoke_request(spool_dir: str, host_id: str, now_ms: int | None = None) -> str:
    """Queue one single-host revoke request for the serving process.

    Creates the spool dir (0700) and writes one JSON file atomically
    (0600): only the host id and the request time, never credentials.
    Returns the request path. Raises ValueError on an empty host id.
    """
    if not host_id or not host_id.strip():
        raise ValueError("host id is required")
    os.makedirs(spool_dir, mode=0o700, exist_ok=True)
    payload = {
        "version": REVOKE_SPOOL_VERSION,
        "host_id": host_id,
        "requested_ms": now_ms if now_ms is not None else int(time.time() * 1000),
    }
    fd, tmp = tempfile.mkstemp(dir=spool_dir, prefix=".revoke-", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(payload, handle)
        os.chmod(tmp, 0o600)
        final = os.path.join(spool_dir, f"revoke-{secrets.token_hex(8)}.json")
        os.replace(tmp, final)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    os.chmod(final, 0o600)
    return final


def drain_revoke_requests(spool_dir: str) -> tuple[str, ...]:
    """Collect and consume queued revoke requests (serving-process side).

    Returns the requested host ids in filename order, deduplicated.
    Malformed files are quarantined to `<name>.bad` (logged, never
    retried forever); `*.tmp` partials are ignored. A missing spool
    dir drains to nothing. Revoke itself stays idempotent, so a crash
    between drain and revoke is safe to replay by re-queuing.
    """
    try:
        names = sorted(os.listdir(spool_dir))
    except FileNotFoundError:
        return ()
    except OSError as exc:
        LOG.warning("revoke spool %s unreadable: %s", spool_dir, exc)
        return ()
    hosts: list[str] = []
    for name in names:
        if name.endswith(".bad") or not name.endswith(".json"):
            continue
        path = os.path.join(spool_dir, name)
        host_id: str | None = None
        try:
            with open(path, encoding="utf-8") as handle:
                raw = json.load(handle)
            if isinstance(raw, dict) and raw.get("version") == REVOKE_SPOOL_VERSION:
                candidate = raw.get("host_id")
                if isinstance(candidate, str) and candidate.strip():
                    host_id = candidate
        except (OSError, ValueError) as exc:
            LOG.warning("revoke request %s unreadable, quarantining: %s", path, exc)
        try:
            if host_id is None:
                os.replace(path, path + ".bad")
            else:
                os.unlink(path)
        except OSError as exc:
            LOG.warning("revoke request %s not consumable: %s", path, exc)
            continue
        if host_id is not None and host_id not in hosts:
            hosts.append(host_id)
    return tuple(hosts)
