"""Versioned, Flet-free contracts and approved limits for private persistence."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Any, Protocol

# Approved retention; runtime integration owns disclosure and activation.
MAX_DRAFT_BYTES = 256 * 1024
MAX_DRAFTS_PER_DEVICE = 128
MAX_DEVICE_BYTES = 8 * 1024 * 1024
MAX_TOTAL_BYTES = 64 * 1024 * 1024
DRAFT_IDLE_MS = 7 * 24 * 60 * 60 * 1000
MAX_SWEEP_ROWS = 128
MAX_NOTICE_ROWS = 1024
MAX_RECORD_BYTES = 256 * 1024
MAX_DATABASE_BYTES = MAX_TOTAL_BYTES * 4
MAX_WAL_BYTES = MAX_TOTAL_BYTES * 8
MAX_AUTHORITY_BYTES = 16 * 1024 * 1024
REVOCATION_RESERVED_BYTES = 8 * 1024 * 1024
AUTHORITY_DISK_RESERVE_BYTES = 8 * 1024 * 1024
RESERVE_BLOCK_BYTES = 64 * 1024
MAX_DRAFT_TOMBSTONES = 128
MAX_NONCE_HISTORY = 1024
MAX_NONCE_SWEEP_ROWS = 512
MAX_RETIRE_KEYS = 3
MAX_SCOPE_PAGE = 128
MAX_QUEUED_DRAFT_OPERATIONS = 32
MAX_SECRET_BYTES = 1024 * 1024
BUSY_TIMEOUT_MS = 1000
RECORD_VERSION = 1


class Table(StrEnum):
    GATEWAY_VERIFIERS = "gateway_verifiers"
    GATEWAY_GRANTS = "gateway_grants"
    WEB_SESSIONS = "web_sessions"
    REVOCATIONS = "revocations"
    DRAFTS = "drafts"
    TAB_ALIASES = "tab_aliases"
    DRAFT_REVISIONS = "draft_revisions"
    EVICTION_NOTICES = "eviction_notices"
    PENDING_FENCES = "pending_fences"


AUTH_EXPIRY_TABLES = frozenset({Table.GATEWAY_VERIFIERS, Table.WEB_SESSIONS, Table.TAB_ALIASES})


@dataclass(frozen=True)
class Record:
    table: Table
    key: str = field(repr=False)
    scope: tuple[str, ...] = field(repr=False)
    body: bytes = field(repr=False)
    revision: int
    updated_ms: int
    expires_ms: int | None = None
    schema_version: int = RECORD_VERSION


@dataclass(frozen=True)
class DraftTarget:
    host: str = field(repr=False)
    terminal: str = field(repr=False)


@dataclass(frozen=True)
class DraftRecord:
    target: DraftTarget
    snapshot: str = field(repr=False)
    revision: int
    updated_ms: int
    schema_version: int = RECORD_VERSION


@dataclass(frozen=True)
class DraftNotice:
    state: str = "saved"
    evicted: int = 0
    expired: int = 0


@dataclass(frozen=True)
class SweepReport:
    expired: int = 0
    evicted: int = 0
    remaining: int = 0
    more: bool = False


@dataclass(frozen=True)
class StorageStatus:
    available: bool
    reason: str = "ready"
    records: int = 0
    bytes_used: int = 0


@dataclass(frozen=True)
class RecordPage:
    records: tuple[Record, ...]
    next_cursor: str | None = None


@dataclass(frozen=True)
class SecretMaterial:
    """Exact imported bytes, distinct from an AES key or a restore generation."""

    value: bytes = field(repr=False)


@dataclass(frozen=True)
class RestoreGeneration:
    value: int


class StorageUnavailable(RuntimeError):
    """Fixed safe categories only; never include a path, identity or exception text."""

    def __init__(self, code: str = "unavailable") -> None:
        allowed = {
            "unavailable",
            "keys_unavailable",
            "corrupt",
            "unknown_schema",
            "unsafe_path",
            "busy",
            "full",
            "closed",
            "invalid_record",
            "revision_conflict",
        }
        self.code = code if code in allowed else "unavailable"
        self.status = StorageStatus(False, self.code)
        super().__init__(self.code)


class KeyUnavailable(RuntimeError):
    def __init__(self) -> None:
        super().__init__("keys_unavailable")


class KeyProvider(Protocol):
    def get(self, name: str, *, create: bool = False, allow_prompt: bool = False) -> bytes: ...
    def retire(self, name: str) -> None:
        """Idempotently delete a non-current encryption key; failure must raise."""
        ...

    def import_secret(
        self, name: str, material: SecretMaterial, *, allow_prompt: bool = False
    ) -> SecretMaterial: ...
    def read_secret(self, name: str) -> SecretMaterial: ...
    def restore_generation(
        self, name: str, *, create: bool = False, allow_prompt: bool = False
    ) -> RestoreGeneration: ...
    def advance_restore_generation(self, name: str) -> RestoreGeneration:
        """Durable serialized increment; an uncertain write never rolls back."""
        ...


class StorageConfig(Protocol):
    @property
    def state_dir(self) -> Path: ...


class RevisionedDraftStore(Protocol):
    async def available(self) -> bool: ...
    async def get(self, key: str) -> str | None: ...
    async def set(self, key: str, value: str) -> None: ...
    async def delete(self, key: str) -> None: ...
    async def clear_account(self) -> None: ...
    async def load(self, target: DraftTarget) -> DraftRecord | None: ...
    async def save(
        self, target: DraftTarget, snapshot: str | Mapping[str, Any], expected_revision: int | None
    ) -> DraftRecord: ...
    async def delete_revision(self, target: DraftTarget, revision: int) -> bool: ...
    def notice(self) -> DraftNotice: ...
