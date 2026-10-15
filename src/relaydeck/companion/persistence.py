"""Standalone private encrypted SQLite storage; no runtime/authentication wiring."""

from __future__ import annotations

import asyncio
import base64
import fcntl
import hashlib
import hmac
import json
import logging
import os
import secrets
import shutil
import sqlite3
import stat
import threading
import time
from collections.abc import Callable, Iterator, Mapping
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from dataclasses import replace
from pathlib import Path
from typing import Any, TypeVar, cast
from urllib.parse import quote

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

from relaydeck import persistence_contracts as limits
from relaydeck.client.adapters.ports import CredentialStore
from relaydeck.contracts.canonical import canonical_json
from relaydeck.persistence_contracts import (
    DraftNotice,
    DraftRecord,
    DraftTarget,
    KeyProvider,
    KeyUnavailable,
    Record,
    RecordPage,
    RevisionedDraftStore,
    StorageConfig,
    StorageStatus,
    StorageUnavailable,
    SweepReport,
    Table,
)

SCHEMA_VERSION = 3
_Result = TypeVar("_Result")
_LOG = logging.getLogger(__name__)
_FILES = (
    "persistence.db",
    "persistence.db-wal",
    "persistence.db-shm",
    "persistence.db-journal",
    "persistence.db.lock",
    "persistence.reserve",
)
_PURPOSES = frozenset({"authority", "web", "drafts"})
_LOOKUP_KEY = "storage.lookup.v1"


def _sqlite_reason(exc: sqlite3.Error) -> str:
    code = getattr(exc, "sqlite_errorcode", 0) & 0xFF
    if code in (sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED):
        return "busy"
    if code in (sqlite3.SQLITE_FULL, sqlite3.SQLITE_TOOBIG):
        return "full"
    if code in (sqlite3.SQLITE_IOERR, sqlite3.SQLITE_CANTOPEN, sqlite3.SQLITE_READONLY):
        return "unavailable"
    return "corrupt"


def _json(value: object) -> bytes:
    return canonical_json(value, ensure_ascii=True).encode()


def _purpose(table: Table) -> str:
    if table in (Table.DRAFTS, Table.DRAFT_REVISIONS, Table.EVICTION_NOTICES):
        return "drafts"
    return "web" if table is Table.WEB_SESSIONS else "authority"


def _private_directory(path: Path, *, create: bool = False) -> int:
    """Walk using directory descriptors; never resolve/follow an ancestor symlink."""
    if ".." in path.parts or not path.is_absolute() or len(path.parts) < 2:
        raise StorageUnavailable("unsafe_path")
    fd = os.open("/", os.O_RDONLY | os.O_DIRECTORY)
    try:
        for index, part in enumerate(path.parts[1:]):
            if create:
                try:
                    os.mkdir(part, mode=0o700, dir_fd=fd)
                except FileExistsError:
                    pass
            child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
            os.close(fd)
            fd = child
            info = os.fstat(fd)
            mode = stat.S_IMODE(info.st_mode)
            shared_temp = info.st_uid == 0 and bool(info.st_mode & stat.S_ISVTX)
            if info.st_uid not in (0, os.getuid()) or (mode & 0o022 and not shared_temp):
                raise StorageUnavailable("unsafe_path")
            if index == len(path.parts) - 2 and (info.st_uid != os.getuid() or mode != 0o700):
                raise StorageUnavailable("unsafe_path")
        return fd
    except (OSError, StorageUnavailable):
        os.close(fd)
        raise StorageUnavailable("unsafe_path") from None


def _check_file(info: os.stat_result) -> None:
    if (
        not stat.S_ISREG(info.st_mode)
        or info.st_uid != os.getuid()
        or info.st_nlink != 1
        or stat.S_IMODE(info.st_mode) != 0o600
    ):
        raise StorageUnavailable("unsafe_path")


def _open_private(fd: int, name: str) -> tuple[int, bool]:
    try:
        try:
            result = os.open(
                name, os.O_RDWR | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=fd
            )
            created = True
        except FileExistsError:
            result = os.open(name, os.O_RDWR | os.O_NOFOLLOW, dir_fd=fd)
            created = False
        try:
            _check_file(os.fstat(result))
            return result, created
        except StorageUnavailable:
            os.close(result)
            raise
    except OSError:
        raise StorageUnavailable("unsafe_path") from None


def _migration_one(db: sqlite3.Connection) -> None:
    db.execute(
        "CREATE TABLE metadata (singleton INTEGER PRIMARY KEY CHECK(singleton=1), "
        "schema_version INTEGER NOT NULL, database_id TEXT NOT NULL, "
        "key_ids TEXT NOT NULL, mac BLOB NOT NULL)"
    )
    db.execute(
        "CREATE TABLE records (table_name TEXT NOT NULL, lookup TEXT NOT NULL, "
        "scope_hash TEXT NOT NULL, device_hash TEXT NOT NULL, revision INTEGER NOT NULL, "
        "updated_ms INTEGER NOT NULL, expires_ms INTEGER, record_version INTEGER NOT NULL, "
        "key_id TEXT NOT NULL, nonce BLOB NOT NULL, ciphertext BLOB NOT NULL, "
        "body_bytes INTEGER NOT NULL, accessed_ms INTEGER NOT NULL, "
        "PRIMARY KEY(table_name, lookup))"
    )
    db.execute(
        "CREATE TABLE nonces (key_id TEXT NOT NULL, nonce BLOB NOT NULL, "
        "PRIMARY KEY(key_id, nonce)) WITHOUT ROWID"
    )


def _migration_two(db: sqlite3.Connection) -> None:
    db.execute("CREATE INDEX record_expiry ON records(table_name, expires_ms, accessed_ms)")
    db.execute("CREATE INDEX record_device ON records(table_name, device_hash, accessed_ms)")


def _migration_three(db: sqlite3.Connection) -> None:
    db.execute("ALTER TABLE metadata ADD COLUMN draft_epoch INTEGER NOT NULL DEFAULT 1")
    db.execute("ALTER TABLE metadata ADD COLUMN draft_sequence INTEGER NOT NULL DEFAULT 0")
    db.execute(
        "CREATE TABLE record_scopes (table_name TEXT NOT NULL, lookup TEXT NOT NULL, "
        "prefix_hash TEXT NOT NULL, PRIMARY KEY(table_name, prefix_hash, lookup)) WITHOUT ROWID"
    )
    db.execute(
        "CREATE TABLE key_generations (key_id TEXT PRIMARY KEY, purpose TEXT NOT NULL, "
        "state TEXT NOT NULL, created_ms INTEGER NOT NULL, mac BLOB NOT NULL)"
    )
    db.execute(
        "CREATE TABLE authority_reserve (block_id INTEGER PRIMARY KEY, payload BLOB NOT NULL)"
    )
    db.execute("CREATE INDEX record_full_scope ON records(table_name, scope_hash)")


_MIGRATIONS: dict[int, Callable[[sqlite3.Connection], None]] = {
    1: _migration_one,
    2: _migration_two,
    3: _migration_three,
}


class PersistentStore:
    def __init__(self, config: StorageConfig, provider: KeyProvider) -> None:
        self._path = config.state_dir.expanduser().absolute()
        self._provider = provider
        self._mutex = threading.Lock()
        self._closed = False
        self._fault: str | None = None
        self._dir_fd = self._db_fd = self._lock_fd = -1
        self._db: sqlite3.Connection
        self._key_ids: dict[str, str] = {}
        self._keys: dict[str, bytes] = {}
        self._root = b""
        self._id = ""
        self._schema = SCHEMA_VERSION
        self._notices: dict[tuple[str, ...], DraftNotice] = {}
        self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="private-drafts")
        self._queued = threading.BoundedSemaphore(limits.MAX_QUEUED_DRAFT_OPERATIONS)
        self._accepting = True
        self._writes_since_maintenance = 0
        try:
            self._dir_fd = _private_directory(self._path, create=True)
            self._check_paths()
            self._lock_fd, _ = _open_private(self._dir_fd, "persistence.db.lock")
            try:
                fcntl.flock(self._lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise StorageUnavailable("busy") from None
            # Provision keys before creating a new database. A locked fresh setup
            # must not leave an empty file that would look like corrupt old data.
            try:
                os.stat("persistence.db", dir_fd=self._dir_fd, follow_symlinks=False)
            except FileNotFoundError:
                self._id = secrets.token_hex(16)
                self._root = self._provider.get(_LOOKUP_KEY, create=True)
                if len(self._root) != 32:
                    raise KeyUnavailable() from None
                self._key_ids, self._keys = self._new_keys()
            self._db_fd, fresh = _open_private(self._dir_fd, "persistence.db")
            if not fresh and os.fstat(self._db_fd).st_size == 0:
                raise StorageUnavailable("corrupt")
            # SQLite sidecars inherit the private DB mode, even under umask=0.
            self._db = sqlite3.connect(
                "file:" + quote(str(self._path / "persistence.db")) + "?mode=rw",
                uri=True,
                timeout=limits.BUSY_TIMEOUT_MS / 1000,
                isolation_level=None,
                check_same_thread=False,
            )
            self._db.row_factory = sqlite3.Row
            self._check_paths()
            self._db.execute(f"PRAGMA busy_timeout={limits.BUSY_TIMEOUT_MS}")
            self._db.execute("PRAGMA temp_store=MEMORY")
            self._db.execute("PRAGMA trusted_schema=OFF")
            self._db.execute("PRAGMA synchronous=FULL")
            self._db.execute("PRAGMA cache_spill=OFF")
            self._db.execute("PRAGMA wal_autocheckpoint=128")
            self._initialize(fresh)
            self._db.execute("PRAGMA journal_mode=WAL")
            page = self._db.execute("PRAGMA page_size").fetchone()[0]
            self._db.execute(f"PRAGMA max_page_count={limits.MAX_DATABASE_BYTES // page}")
            if self._schema >= 3:
                self._disk_reserve()
            self._restore_notices()
            self._check_paths()
            self.sweep(self._now_ms())
        except (
            StorageUnavailable,
            sqlite3.Error,
            KeyUnavailable,
            OSError,
            ValueError,
            RuntimeError,
        ) as exc:
            self.close()
            if isinstance(exc, StorageUnavailable):
                raise
            code = (
                "keys_unavailable"
                if isinstance(exc, KeyUnavailable)
                else _sqlite_reason(exc)
                if isinstance(exc, sqlite3.Error)
                else "corrupt"
            )
            raise StorageUnavailable(code) from None

    def _now_ms(self) -> int:
        return time.time_ns() // 1_000_000

    async def run_draft(self, operation: Callable[[], _Result]) -> _Result:
        """Whole transactions run on the single worker, including Keychain calls."""
        if not self._accepting or not self._queued.acquire(blocking=False):
            raise StorageUnavailable("closed" if not self._accepting else "busy")
        try:

            def execute() -> _Result:
                try:
                    return operation()
                finally:
                    self._queued.release()

            future = asyncio.get_running_loop().run_in_executor(self._executor, execute)
        except RuntimeError:
            self._queued.release()
            raise StorageUnavailable("closed") from None
        # Cancellation must not free a queue slot while its actual transaction runs.
        future.add_done_callback(
            lambda completed: completed.exception() if not completed.cancelled() else None
        )
        return await asyncio.shield(future)

    def _restore_notices(self) -> None:
        tx = Transaction(self)
        for row in self._db.execute(
            "SELECT * FROM records WHERE table_name=? LIMIT ?",
            (Table.EVICTION_NOTICES.value, limits.MAX_NOTICE_ROWS + 1),
        ).fetchall():
            record = tx._decode(row)
            self._notices[record.scope] = _notice_value(record.body)
        if len(self._notices) > limits.MAX_NOTICE_ROWS:
            raise StorageUnavailable("corrupt")
        tx._active = False

    def _disk_reserve(self) -> None:
        fd, created = _open_private(self._dir_fd, "persistence.reserve")
        try:
            if created:
                # Write allocated bytes, not sparse truncate-only space.
                block = bytes(limits.RESERVE_BLOCK_BYTES)
                remaining = limits.AUTHORITY_DISK_RESERVE_BYTES
                while remaining:
                    written = os.write(fd, block[:remaining])
                    if written <= 0:
                        raise StorageUnavailable("full")
                    remaining -= written
                os.fsync(fd)
                os.fsync(self._dir_fd)
        finally:
            os.close(fd)

    def _release_disk_reserve(self, needed: int) -> None:
        fd, _ = _open_private(self._dir_fd, "persistence.reserve")
        try:
            size = os.fstat(fd).st_size
            if size < needed:
                raise StorageUnavailable("full")
            os.ftruncate(fd, size - needed)
            os.fsync(fd)
        finally:
            os.close(fd)

    def _check_paths(self) -> None:
        fd = _private_directory(self._path)
        try:
            current, original = os.fstat(fd), os.fstat(self._dir_fd)
            if (current.st_dev, current.st_ino) != (original.st_dev, original.st_ino):
                raise StorageUnavailable("unsafe_path")
            for name in _FILES:
                try:
                    info = os.stat(name, dir_fd=fd, follow_symlinks=False)
                except FileNotFoundError:
                    if name == "persistence.db" and self._db_fd >= 0:
                        raise StorageUnavailable("unsafe_path") from None
                    continue
                _check_file(info)
                cap = limits.MAX_WAL_BYTES if name.endswith("-wal") else limits.MAX_DATABASE_BYTES
                if info.st_size > cap:
                    raise StorageUnavailable("full")
                if name == "persistence.db" and self._db_fd >= 0:
                    if (info.st_dev, info.st_ino) != (
                        os.fstat(self._db_fd).st_dev,
                        os.fstat(self._db_fd).st_ino,
                    ):
                        raise StorageUnavailable("unsafe_path")
        finally:
            os.close(fd)

    def _derive(self, label: str) -> bytes:
        return HKDF(
            algorithm=hashes.SHA256(),
            length=32,
            salt=self._id.encode(),
            info=("relaydeck.persistence." + label).encode(),
        ).derive(self._root)

    def _hash(self, label: str, value: object) -> str:
        return hmac.new(self._derive("lookup"), _json([label, value]), hashlib.sha256).hexdigest()

    def _clock(self) -> tuple[int, int]:
        if self._schema < 3:
            return 1, 0
        row = self._db.execute(
            "SELECT draft_epoch, draft_sequence FROM metadata WHERE singleton=1"
        ).fetchone()
        return (row[0], row[1]) if row else (1, 0)

    def _meta_mac(
        self,
        version: int,
        key_ids: dict[str, str],
        *,
        epoch: int | None = None,
        sequence: int | None = None,
    ) -> bytes:
        data: list[object] = [version, self._id, key_ids]
        if version >= 3:
            current_epoch, current_sequence = self._clock()
            data += [
                current_epoch if epoch is None else epoch,
                current_sequence if sequence is None else sequence,
            ]
        return hmac.new(self._derive("metadata"), _json(data), hashlib.sha256).digest()

    def _generation_mac(self, key_id: str, purpose: str, state: str, created: int) -> bytes:
        return hmac.new(
            self._derive("generation"), _json([key_id, purpose, state, created]), hashlib.sha256
        ).digest()

    def _set_generation(self, key_id: str, purpose: str, state: str, created: int) -> None:
        self._db.execute(
            "INSERT OR REPLACE INTO key_generations VALUES(?,?,?,?,?)",
            (
                key_id,
                purpose,
                state,
                created,
                self._generation_mac(key_id, purpose, state, created),
            ),
        )

    def _generation(self, key_id: str) -> sqlite3.Row:
        row = self._db.execute("SELECT * FROM key_generations WHERE key_id=?", (key_id,)).fetchone()
        if (
            row is None
            or row["purpose"] not in _PURPOSES
            or row["state"] not in ("active", "draining", "retired")
            or not hmac.compare_digest(
                row["mac"],
                self._generation_mac(key_id, row["purpose"], row["state"], row["created_ms"]),
            )
        ):
            raise StorageUnavailable("corrupt")
        return cast(sqlite3.Row, row)

    def _new_keys(self) -> tuple[dict[str, str], dict[str, bytes]]:
        ids = {p: f"storage.{self._id}.{p}.{secrets.token_hex(16)}" for p in sorted(_PURPOSES)}
        keys = {p: self._provider.get(name, create=True) for p, name in ids.items()}
        if any(len(key) != 32 for key in keys.values()):
            raise KeyUnavailable()
        return ids, keys

    def _validate_keys(self) -> None:
        try:
            if not hmac.compare_digest(self._provider.get(_LOOKUP_KEY), self._root):
                raise KeyUnavailable()
            for purpose, name in self._key_ids.items():
                key = self._provider.get(name)
                if not hmac.compare_digest(key, self._keys[purpose]):
                    raise KeyUnavailable()
        except KeyUnavailable:
            raise StorageUnavailable("keys_unavailable") from None

    def _validate_metadata(self, key_ids: dict[str, str] | None = None) -> None:
        version = self._db.execute("PRAGMA user_version").fetchone()[0]
        if version != self._schema:
            raise StorageUnavailable("unknown_schema" if version > self._schema else "corrupt")
        meta = self._db.execute("SELECT * FROM metadata WHERE singleton=1").fetchone()
        expected = self._key_ids if key_ids is None else key_ids
        try:
            if (
                meta is None
                or meta["schema_version"] != self._schema
                or meta["database_id"] != self._id
                or json.loads(meta["key_ids"]) != expected
                or not hmac.compare_digest(meta["mac"], self._meta_mac(self._schema, expected))
            ):
                raise ValueError
        except (TypeError, ValueError):
            raise StorageUnavailable("corrupt") from None
        if self._schema >= 3:
            epoch, sequence = self._clock()
            if not 1 <= epoch < 2**31 or not 0 <= sequence < 2**32:
                raise StorageUnavailable("corrupt")
            for p, key_id in expected.items():
                generation = self._generation(key_id)
                if generation["state"] != "active" or generation["purpose"] != p:
                    raise StorageUnavailable("corrupt")

    def _initialize(self, fresh: bool) -> None:
        version = self._db.execute("PRAGMA user_version").fetchone()[0]
        if version > SCHEMA_VERSION or version < 0:
            raise StorageUnavailable("unknown_schema")
        if self._db.execute("PRAGMA quick_check(1)").fetchone()[0] != "ok":
            raise StorageUnavailable("corrupt")
        if version == 0:
            if not fresh or self._db.execute("SELECT name FROM sqlite_master").fetchone():
                raise StorageUnavailable("corrupt")
            if not self._id or not self._keys:
                raise StorageUnavailable("corrupt")
        else:
            meta = self._db.execute("SELECT * FROM metadata WHERE singleton=1").fetchone()
            if meta is None or meta["schema_version"] != version:
                raise StorageUnavailable("corrupt")
            self._id = meta["database_id"]
            if not isinstance(self._id, str) or len(self._id) != 32:
                raise StorageUnavailable("corrupt")
            self._root = self._provider.get(_LOOKUP_KEY)
            self._key_ids = json.loads(meta["key_ids"])
            if (
                not isinstance(self._key_ids, dict)
                or set(self._key_ids) != _PURPOSES
                or any(
                    not isinstance(v, str) or not v.startswith(f"storage.{self._id}.{p}.")
                    for p, v in self._key_ids.items()
                )
                or not hmac.compare_digest(meta["mac"], self._meta_mac(version, self._key_ids))
            ):
                raise StorageUnavailable("corrupt")
            self._keys = {p: self._provider.get(n) for p, n in self._key_ids.items()}
        if len(self._root) != 32 or any(len(k) != 32 for k in self._keys.values()):
            raise KeyUnavailable()
        self._db.execute("BEGIN IMMEDIATE")
        try:
            for target in range(version + 1, SCHEMA_VERSION + 1):
                _MIGRATIONS[target](self._db)
                self._db.execute(f"PRAGMA user_version={target}")
            columns = "singleton,schema_version,database_id,key_ids,mac"
            values: tuple[object, ...] = (
                1,
                SCHEMA_VERSION,
                self._id,
                _json(self._key_ids).decode(),
                self._meta_mac(SCHEMA_VERSION, self._key_ids),
            )
            if self._schema >= 3:
                columns += ",draft_epoch,draft_sequence"
                values += self._clock()
            self._db.execute(
                f"INSERT OR REPLACE INTO metadata ({columns}) VALUES "
                + "("
                + ",".join("?" for _ in values)
                + ")",
                values,
            )
            if version < 3 <= self._schema:
                for p, key_id in self._key_ids.items():
                    self._set_generation(key_id, p, "active", self._now_ms())
                tx = Transaction(self)
                for row in self._db.execute("SELECT * FROM records").fetchall():
                    tx._index(tx._decode(row))
                for index in range(
                    limits.AUTHORITY_DISK_RESERVE_BYTES // limits.RESERVE_BLOCK_BYTES
                ):
                    self._db.execute(
                        "INSERT INTO authority_reserve VALUES(?, zeroblob(?))",
                        (index, limits.RESERVE_BLOCK_BYTES),
                    )
            self._validate_keys()
            self._db.execute("COMMIT")
        except BaseException:
            self._db.execute("ROLLBACK")
            raise

    @contextmanager
    def transaction(self) -> Iterator[Transaction]:
        if not self._mutex.acquire(timeout=limits.BUSY_TIMEOUT_MS / 1000):
            raise StorageUnavailable("busy")
        tx: Transaction | None = None
        try:
            if self._closed or self._fault:
                raise StorageUnavailable(self._fault or "closed")
            self._check_paths()
            self._validate_keys()
            self._db.execute("BEGIN IMMEDIATE")
            tx = Transaction(self)
            self._validate_metadata()
            yield tx
            self._check_paths()
            self._validate_keys()
            self._validate_metadata(tx._replacement[0] if tx._replacement else None)
            for name, key in tx._used_keys.items():
                try:
                    if not hmac.compare_digest(self._provider.get(name), key):
                        raise KeyUnavailable()
                except KeyUnavailable:
                    raise StorageUnavailable("keys_unavailable") from None
            if tx._replacement is not None:
                ids, keys = tx._replacement
                for p, name in ids.items():
                    try:
                        if not hmac.compare_digest(self._provider.get(name), keys[p]):
                            raise KeyUnavailable()
                    except KeyUnavailable:
                        raise StorageUnavailable("keys_unavailable") from None
            self._db.execute("COMMIT")
            if tx._replacement is not None:
                self._key_ids, self._keys = tx._replacement
            self._notices.update(tx._notice_changes)
        except BaseException as exc:
            if not self._closed and hasattr(self, "_db") and self._db.in_transaction:
                try:
                    self._db.execute("ROLLBACK")
                except sqlite3.Error:
                    self._fault = "corrupt"
                    raise StorageUnavailable("corrupt") from None
            if isinstance(exc, sqlite3.Error):
                code = _sqlite_reason(exc)
                if code == "corrupt":
                    self._fault = code
                raise StorageUnavailable(code) from None
            if isinstance(exc, StorageUnavailable) and exc.code in (
                "corrupt",
                "unsafe_path",
                "unknown_schema",
            ):
                self._fault = exc.code
            raise
        finally:
            if tx:
                tx._active = False
            self._mutex.release()

    def status(self) -> StorageStatus:
        try:
            with self.transaction() as tx:
                count, size = tx._db.execute(
                    "SELECT count(*), coalesce(sum(body_bytes),0) FROM records"
                ).fetchone()
            return StorageStatus(True, records=count, bytes_used=size)
        except StorageUnavailable as exc:
            return exc.status

    def sweep(self, now_ms: int) -> SweepReport:
        if now_ms < 0:
            raise StorageUnavailable("invalid_record")
        auth = tuple(t.value for t in limits.AUTH_EXPIRY_TABLES)
        params = (*auth, now_ms, Table.DRAFTS.value, now_ms - limits.DRAFT_IDLE_MS)
        condition = "(table_name IN (?,?,?) AND expires_ms<=?) OR (table_name=? AND accessed_ms<=?)"
        with self.transaction() as tx:
            rows = tx._db.execute(
                "SELECT * FROM records WHERE " + condition + " ORDER BY updated_ms, lookup LIMIT ?",
                (*params, limits.MAX_SWEEP_ROWS),
            ).fetchall()
            for row in rows:
                record = tx._decode(row)
                if record.table is Table.DRAFTS:
                    tx._notice(record.scope[:3], expired=1)
                tx.delete(record.table, record.key, expected_revision=record.revision)
            remaining = tx._db.execute("SELECT count(*) FROM records").fetchone()[0]
            more = (
                tx._db.execute(
                    "SELECT 1 FROM records WHERE " + condition + " LIMIT 1", params
                ).fetchone()
                is not None
            )
        if self._schema >= 3:
            removed = self._prune_counters(now_ms)
            self.maintain(force_drafts=bool(removed))
            with self.transaction() as tx:
                remaining = tx._db.execute("SELECT count(*) FROM records").fetchone()[0]
                more = (
                    more
                    or bool(
                        tx._db.execute(
                            "SELECT 1 FROM key_generations WHERE state<>'active' LIMIT 1"
                        ).fetchone()
                    )
                    or bool(
                        tx._db.execute(
                            "SELECT 1 FROM records c WHERE c.table_name=? AND c.updated_ms<=? "
                            "AND NOT EXISTS (SELECT 1 FROM records d WHERE d.table_name=? "
                            "AND d.scope_hash=c.scope_hash) LIMIT 1",
                            (
                                Table.DRAFT_REVISIONS.value,
                                now_ms - limits.DRAFT_IDLE_MS,
                                Table.DRAFTS.value,
                            ),
                        ).fetchone()
                    )
                )
        _LOG.info("storage sweep expired=%d remaining=%d", len(rows), remaining)
        return SweepReport(expired=len(rows), remaining=remaining, more=more)

    def _prune_counters(self, now_ms: int) -> int:
        removed = 0
        with self.transaction() as tx:
            missing = (
                "c.table_name=? AND NOT EXISTS (SELECT 1 FROM records d "
                "WHERE d.table_name=? AND d.scope_hash=c.scope_hash)"
            )
            params = (Table.DRAFT_REVISIONS.value, Table.DRAFTS.value)
            count = tx._db.execute(
                "SELECT count(*) FROM records c WHERE " + missing, params
            ).fetchone()[0]
            rows = tx._db.execute(
                "SELECT c.* FROM records c WHERE "
                + missing
                + " ORDER BY c.updated_ms, c.lookup LIMIT ?",
                (*params, limits.MAX_SWEEP_ROWS),
            ).fetchall()
            for row in rows:
                if (
                    count - removed <= limits.MAX_DRAFT_TOMBSTONES
                    and row["updated_ms"] > now_ms - limits.DRAFT_IDLE_MS
                ):
                    continue
                counter = tx._decode(row)
                if tx.get(Table.DRAFTS, counter.key) is None:
                    tx.delete(counter.table, counter.key, expected_revision=counter.revision)
                    removed += 1
        return removed

    def _begin_rotation(self, purpose: str) -> None:
        with self.transaction() as tx:
            if tx._db.execute(
                "SELECT 1 FROM key_generations WHERE purpose=? AND state<>'active'", (purpose,)
            ).fetchone():
                return
            old_id = self._key_ids[purpose]
            old = self._generation(old_id)
            new_id = f"storage.{self._id}.{purpose}.{secrets.token_hex(16)}"
            try:
                key = self._provider.get(new_id, create=True)
                if len(key) != 32:
                    raise KeyUnavailable()
            except KeyUnavailable:
                raise StorageUnavailable("keys_unavailable") from None
            ids, keys = dict(self._key_ids), dict(self._keys)
            ids[purpose], keys[purpose] = new_id, key
            self._set_generation(old_id, purpose, "draining", old["created_ms"])
            self._set_generation(new_id, purpose, "active", self._now_ms())
            tx._db.execute(
                "UPDATE metadata SET key_ids=?, mac=? WHERE singleton=1",
                (_json(ids).decode(), self._meta_mac(self._schema, ids)),
            )
            tx._replacement = ids, keys

    def maintain(self, *, force_drafts: bool = False) -> None:
        """Bounded drain, then key deletion, then bounded nonce reclamation."""
        if self._schema < 3:
            return
        with self.transaction() as tx:
            extras = {
                p: tx._db.execute(
                    "SELECT count(*) FROM nonces WHERE key_id=?", (key_id,)
                ).fetchone()[0]
                - tx._db.execute(
                    "SELECT count(*) FROM records WHERE key_id=?", (key_id,)
                ).fetchone()[0]
                for p, key_id in self._key_ids.items()
            }
        for p, extra in extras.items():
            if extra >= limits.MAX_NONCE_HISTORY or (force_drafts and p == "drafts"):
                self._begin_rotation(p)
        with self.transaction() as tx:
            generations = tx._db.execute(
                "SELECT key_id FROM key_generations WHERE state<>'active' "
                "ORDER BY created_ms, key_id LIMIT ?",
                (limits.MAX_RETIRE_KEYS,),
            ).fetchall()
            budget = limits.MAX_SWEEP_ROWS
            for entry in generations:
                key_id = entry[0]
                generation = self._generation(key_id)
                rows = tx._db.execute(
                    "SELECT * FROM records WHERE key_id=? LIMIT ?", (key_id, budget)
                ).fetchall()
                if generation["state"] == "retired" and rows:
                    raise StorageUnavailable("corrupt")
                for row in rows:
                    tx._write(tx._decode(row), accessed_ms=row["accessed_ms"])
                budget -= len(rows)
        # A retirement never occurs in the transaction that still needs the old
        # decryption key at commit. Interrupted deletion is idempotent next time.
        with self.transaction() as tx:
            budget = limits.MAX_NONCE_SWEEP_ROWS
            for entry in generations:
                key_id = entry[0]
                generation = self._generation(key_id)
                if tx._db.execute(
                    "SELECT 1 FROM records WHERE key_id=? LIMIT 1", (key_id,)
                ).fetchone():
                    continue
                if key_id in self._key_ids.values():
                    raise StorageUnavailable("corrupt")
                if generation["state"] != "retired":
                    try:
                        self._provider.retire(key_id)
                    except KeyUnavailable:
                        raise StorageUnavailable("keys_unavailable") from None
                    self._set_generation(
                        key_id, generation["purpose"], "retired", generation["created_ms"]
                    )
                deleted = tx._db.execute(
                    "DELETE FROM nonces WHERE key_id=? AND nonce IN "
                    "(SELECT nonce FROM nonces WHERE key_id=? LIMIT ?)",
                    (key_id, key_id, budget),
                ).rowcount
                budget -= deleted
                if not tx._db.execute(
                    "SELECT 1 FROM nonces WHERE key_id=? LIMIT 1", (key_id,)
                ).fetchone():
                    tx._db.execute("DELETE FROM key_generations WHERE key_id=?", (key_id,))
        excess = self._draft_nonce_excess() if self._schema >= 3 else 0
        self._writes_since_maintenance = (
            max(self._writes_since_maintenance, limits.MAX_SWEEP_ROWS * 2)
            if excess >= limits.MAX_NONCE_HISTORY
            else 0
        )

    def _draft_nonce_excess(self) -> int:
        with self.transaction() as tx:
            key = self._key_ids["drafts"]
            return int(
                tx._db.execute(
                    "SELECT (SELECT count(*) FROM nonces WHERE key_id=?) - "
                    "(SELECT count(*) FROM records WHERE key_id=?)",
                    (key, key),
                ).fetchone()[0]
            )

    def _preflight_drafts(self, writes: int) -> None:
        if self._schema < 3 or writes == 0:
            return
        # Each pass is bounded. A retired key's remaining history may need two
        # reclamation passes before the next active generation can rotate.
        for _ in range(limits.MAX_RETIRE_KEYS + 1):
            if self._draft_nonce_excess() + writes <= limits.MAX_NONCE_HISTORY:
                return
            self.maintain(force_drafts=True)
        # Keep maintenance due on retry, even for large draining generations.
        self._writes_since_maintenance = max(
            self._writes_since_maintenance, limits.MAX_SWEEP_ROWS * 2
        )
        raise StorageUnavailable("full")

    def scoped_drafts(
        self, origin: str, account: str, device: str, tab: str
    ) -> RevisionedDraftStore:
        scope = (origin, account, device, tab)
        _validate_scope(scope)
        return _DraftStore(self, scope)

    def rotate_keys(self) -> None:
        """Atomic re-encryption. Previous Keychain items are never deleted here."""
        try:
            with self.transaction() as tx:
                new_ids, new_keys = self._new_keys()
                for row in tx._db.execute("SELECT * FROM records").fetchall():
                    record = tx._decode(row)
                    tx._write(
                        record, key_ids=new_ids, keys=new_keys, accessed_ms=row["accessed_ms"]
                    )
                # Recheck both generations before publishing the new metadata.
                for p, name in new_ids.items():
                    if not hmac.compare_digest(self._provider.get(name), new_keys[p]):
                        raise StorageUnavailable("keys_unavailable")
                if self._schema >= 3:
                    for p, old_id in self._key_ids.items():
                        old = self._generation(old_id)
                        self._set_generation(old_id, p, "draining", old["created_ms"])
                        self._set_generation(new_ids[p], p, "active", self._now_ms())
                tx._db.execute(
                    "UPDATE metadata SET key_ids=?, mac=? WHERE singleton=1",
                    (_json(new_ids).decode(), self._meta_mac(SCHEMA_VERSION, new_ids)),
                )
                tx._replacement = new_ids, new_keys
        except KeyUnavailable:
            raise StorageUnavailable("keys_unavailable") from None

    def close(self) -> None:
        if self._closed:
            return
        self._accepting = False
        self._executor.shutdown(wait=True, cancel_futures=False)
        if not self._mutex.acquire(timeout=limits.BUSY_TIMEOUT_MS / 1000):
            raise StorageUnavailable("busy")
        try:
            if self._closed:
                return
            if hasattr(self, "_db"):
                try:
                    # Maintenance only, never claimed as secure erasure.
                    if shutil.disk_usage(self._path).free > limits.MAX_WAL_BYTES * 2:
                        self._db.execute("PRAGMA wal_checkpoint(TRUNCATE)")
                except (OSError, sqlite3.Error):
                    pass
                self._db.close()
            for fd in (self._db_fd, self._lock_fd, self._dir_fd):
                if fd >= 0:
                    os.close(fd)
            self._closed = True
        finally:
            self._mutex.release()


def _validate_scope(scope: tuple[str, ...]) -> None:
    if (
        not isinstance(scope, tuple)
        or not scope
        or len(scope) > 8
        or any(not isinstance(s, str) or not s or len(s.encode()) > 2048 for s in scope)
    ):
        raise StorageUnavailable("invalid_record")


def _notice_value(body: bytes) -> DraftNotice:
    try:
        values = json.loads(body)
        if (
            not isinstance(values, dict)
            or set(values) != {"expired", "evicted"}
            or any(type(v) is not int or v < 0 for v in values.values())
        ):
            raise ValueError
        return DraftNotice(
            "evicted" if any(values.values()) else "saved", values["evicted"], values["expired"]
        )
    except (ValueError, TypeError):
        raise StorageUnavailable("corrupt") from None


def _validate(record: Record) -> None:
    _validate_scope(record.scope)
    if (
        not isinstance(record.table, Table)
        or not isinstance(record.key, str)
        or not record.key
        or len(record.key.encode()) > 16384
        or not isinstance(record.body, bytes)
        or record.schema_version != limits.RECORD_VERSION
        or record.revision < 1
        or record.updated_ms < 0
        or (record.expires_ms is not None and record.expires_ms < 0)
        or (record.table in limits.AUTH_EXPIRY_TABLES and record.expires_ms is None)
        or (record.table is Table.DRAFTS and len(record.scope) != 6)
    ):
        raise StorageUnavailable("invalid_record")
    if len(record.body) > limits.MAX_RECORD_BYTES:
        raise StorageUnavailable("full")


class Transaction:
    def __init__(self, store: PersistentStore) -> None:
        self._store, self._db = store, store._db
        self._active = True
        self._charged = 0
        self._replacement: tuple[dict[str, str], dict[str, bytes]] | None = None
        self._used_keys: dict[str, bytes] = {}
        self._notice_changes: dict[tuple[str, ...], DraftNotice] = {}

    def _next_revision(self) -> int:
        epoch, sequence = self._store._clock()
        if sequence == 2**32 - 1:
            epoch, sequence = epoch + 1, 0
        sequence += 1
        self._set_clock(epoch, sequence)
        return (epoch << 32) | sequence

    def _advance_epoch(self) -> None:
        epoch, _ = self._store._clock()
        self._set_clock(epoch + 1, 0)

    def _set_clock(self, epoch: int, sequence: int) -> None:
        if not 1 <= epoch < 2**31:
            raise StorageUnavailable("full")
        mac = self._store._meta_mac(
            self._store._schema, self._store._key_ids, epoch=epoch, sequence=sequence
        )
        self._db.execute(
            "UPDATE metadata SET draft_epoch=?, draft_sequence=?, mac=? WHERE singleton=1",
            (epoch, sequence, mac),
        )

    def _index(self, record: Record) -> None:
        if self._store._schema < 3:
            return
        lookup = self._lookup(record.table, record.key)
        self._db.execute(
            "DELETE FROM record_scopes WHERE table_name=? AND lookup=?",
            (record.table.value, lookup),
        )
        self._db.executemany(
            "INSERT INTO record_scopes VALUES(?,?,?)",
            [
                (record.table.value, lookup, self._store._hash("prefix", record.scope[:length]))
                for length in range(1, len(record.scope) + 1)
            ],
        )

    def scan(
        self, table: Table, *, scope: tuple[str, ...], limit: int = 128, after: str | None = None
    ) -> RecordPage:
        self._check()
        _validate_scope(scope)
        if (
            not isinstance(table, Table)
            or not 1 <= limit <= limits.MAX_SCOPE_PAGE
            or (
                after is not None
                and (len(after) != 64 or any(c not in "0123456789abcdef" for c in after))
            )
        ):
            raise StorageUnavailable("invalid_record")
        rows = self._db.execute(
            "SELECT r.* FROM records r JOIN record_scopes s "
            "ON r.table_name=s.table_name AND r.lookup=s.lookup "
            "WHERE s.table_name=? AND s.prefix_hash=? AND r.lookup>? ORDER BY r.lookup LIMIT ?",
            (table.value, self._store._hash("prefix", scope), after or "", limit + 1),
        ).fetchall()
        records = tuple(self._decode(row) for row in rows[:limit])
        if any(record.scope[: len(scope)] != scope for record in records):
            raise StorageUnavailable("corrupt")
        return RecordPage(records, rows[limit - 1]["lookup"] if len(rows) > limit else None)

    def _check(self) -> None:
        if not self._active:
            raise StorageUnavailable("closed")

    def _lookup(self, table: Table, key: str) -> str:
        if not isinstance(table, Table) or not isinstance(key, str) or not key:
            raise StorageUnavailable("invalid_record")
        return self._store._hash("record", [table.value, key])

    def _header(self, record: Record, key_id: str, accessed_ms: int) -> tuple[object, ...]:
        device_scope = record.scope[:3] if record.table is Table.DRAFTS else record.scope
        return (
            record.table.value,
            self._lookup(record.table, record.key),
            self._store._hash("scope", record.scope),
            self._store._hash("device", device_scope),
            record.revision,
            record.updated_ms,
            record.expires_ms,
            record.schema_version,
            key_id,
            len(record.body),
            accessed_ms,
        )

    def _decode(self, row: sqlite3.Row) -> Record:
        try:
            table = Table(row["table_name"])
            purpose = _purpose(table)
            if row["key_id"] != self._store._key_ids[purpose]:
                generation = self._store._generation(row["key_id"])
                if generation["purpose"] != purpose or generation["state"] != "draining":
                    raise ValueError
                try:
                    key = self._store._provider.get(row["key_id"])
                except KeyUnavailable:
                    raise StorageUnavailable("keys_unavailable") from None
                self._used_keys[row["key_id"]] = key
            else:
                key = self._store._keys[purpose]
            header = tuple(
                row[k]
                for k in (
                    "table_name",
                    "lookup",
                    "scope_hash",
                    "device_hash",
                    "revision",
                    "updated_ms",
                    "expires_ms",
                    "record_version",
                    "key_id",
                    "body_bytes",
                    "accessed_ms",
                )
            )
            raw = AESGCM(key).decrypt(
                row["nonce"], row["ciphertext"], _json([self._store._id, header])
            )
            data = json.loads(raw)
            if not isinstance(data, dict) or set(data) != {"key", "scope", "body"}:
                raise ValueError
            record = Record(
                table,
                data["key"],
                tuple(data["scope"]),
                base64.b64decode(data["body"], validate=True),
                row["revision"],
                row["updated_ms"],
                row["expires_ms"],
                row["record_version"],
            )
            _validate(record)
            if (
                row["accessed_ms"] < 0
                or self._header(record, row["key_id"], row["accessed_ms"]) != header
            ):
                raise ValueError
            return record
        except StorageUnavailable:
            raise
        except (InvalidTag, ValueError, TypeError, KeyError):
            raise StorageUnavailable("corrupt") from None

    def get(self, table: Table, key: str) -> Record | None:
        self._check()
        row = self._db.execute(
            "SELECT * FROM records WHERE table_name=? AND lookup=?",
            (table.value, self._lookup(table, key)),
        ).fetchone()
        return self._decode(row) if row is not None else None

    def _charge(self, size: int, *, authority: bool = False) -> None:
        # Conservative write budget; dirty pages stay in memory until commit.
        self._charged += size * 3 + 16384
        try:
            wal = os.stat("persistence.db-wal", dir_fd=self._store._dir_fd).st_size
        except FileNotFoundError:
            wal = 0
        ceiling = limits.MAX_WAL_BYTES if authority else limits.MAX_WAL_BYTES // 2
        if wal + self._charged > ceiling:
            raise StorageUnavailable("full")

    def _write(
        self,
        record: Record,
        *,
        key_ids: dict[str, str] | None = None,
        keys: dict[str, bytes] | None = None,
        accessed_ms: int | None = None,
    ) -> None:
        self._check()
        self._charge(
            len(record.body),
            authority=record.table
            not in (Table.DRAFTS, Table.DRAFT_REVISIONS, Table.EVICTION_NOTICES),
        )
        ids, material = key_ids or self._store._key_ids, keys or self._store._keys
        purpose = _purpose(record.table)
        key_id = ids[purpose]
        if self._store._schema >= 3 and purpose == "drafts":
            if shutil.disk_usage(self._store._path).free < len(record.body) * 2 + 32768:
                raise StorageUnavailable("full")
            used = self._db.execute(
                "SELECT count(*) FROM nonces WHERE key_id=?", (key_id,)
            ).fetchone()[0]
            live = self._db.execute(
                "SELECT count(*) FROM records WHERE key_id=?", (key_id,)
            ).fetchone()[0]
            if used - live >= limits.MAX_NONCE_HISTORY:
                raise StorageUnavailable("full")
        for _ in range(8):
            nonce = secrets.token_bytes(12)
            cursor = self._db.execute("INSERT OR IGNORE INTO nonces VALUES(?, ?)", (key_id, nonce))
            if cursor.rowcount:
                break
        else:
            raise StorageUnavailable("full")
        header = self._header(
            record, key_id, record.updated_ms if accessed_ms is None else accessed_ms
        )
        raw = _json(
            {
                "key": record.key,
                "scope": record.scope,
                "body": base64.b64encode(record.body).decode(),
            }
        )
        encrypted = AESGCM(material[purpose]).encrypt(nonce, raw, _json([self._store._id, header]))
        self._db.execute(
            "INSERT OR REPLACE INTO records VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (*header[:9], nonce, encrypted, *header[9:]),
        )
        self._index(record)
        self._store._writes_since_maintenance += 1

    def put(self, record: Record, *, expected_revision: int | None) -> bool:
        self._check()
        _validate(record)
        # A failed single operation cannot leave eviction/counter side effects behind,
        # even when a caller catches its exception inside the outer transaction.
        self._db.execute("SAVEPOINT record_put")
        notices_before = dict(self._notice_changes)
        try:
            old = self.get(record.table, record.key)
            if (old is None and expected_revision is not None) or (
                old is not None and old.revision != expected_revision
            ):
                self._db.execute("RELEASE record_put")
                return False
            if old is not None and (record.revision <= old.revision or record.scope != old.scope):
                raise StorageUnavailable("invalid_record")
            if record.table is Table.DRAFTS:
                counter = self.get(Table.DRAFT_REVISIONS, record.key)
                if counter is not None and record.revision <= counter.revision:
                    raise StorageUnavailable("revision_conflict")
                self._quota(record, old)
                epoch, sequence = self._store._clock()
                if sequence == 0 or record.revision != (epoch << 32) | sequence:
                    raise StorageUnavailable("invalid_record")
                self._write(replace(record, table=Table.DRAFT_REVISIONS, body=b""))
            elif record.table not in (Table.DRAFT_REVISIONS, Table.EVICTION_NOTICES):
                self._authority_capacity(record, old)
            self._write(record)
            self._db.execute("RELEASE record_put")
            return True
        except BaseException:
            self._notice_changes = notices_before
            if self._db.in_transaction:
                self._db.execute("ROLLBACK TO record_put")
                self._db.execute("RELEASE record_put")
            raise

    def _authority_capacity(self, record: Record, old: Record | None) -> None:
        tables = (Table.DRAFTS.value, Table.DRAFT_REVISIONS.value, Table.EVICTION_NOTICES.value)
        total = self._db.execute(
            "SELECT coalesce(sum(body_bytes),0) FROM records WHERE table_name NOT IN (?,?,?)",
            tables,
        ).fetchone()[0]
        cap = limits.MAX_AUTHORITY_BYTES
        if record.table is not Table.REVOCATIONS:
            cap -= limits.REVOCATION_RESERVED_BYTES
        if total - (len(old.body) if old else 0) + len(record.body) > cap:
            raise StorageUnavailable("full")
        self._authority_space(len(record.body))

    def _authority_space(self, size: int) -> None:
        if self._store._schema < 3:
            return
        needed = max(limits.RESERVE_BLOCK_BYTES, size * 2 + 32768)
        if shutil.disk_usage(self._store._path).free < needed:
            self._store._release_disk_reserve(needed)
        page_size = self._db.execute("PRAGMA page_size").fetchone()[0]
        free = self._db.execute("PRAGMA freelist_count").fetchone()[0] * page_size
        maximum = self._db.execute("PRAGMA max_page_count").fetchone()[0] * page_size
        current = self._db.execute("PRAGMA page_count").fetchone()[0] * page_size
        if maximum - current + free < needed:
            blocks = (
                needed - (maximum - current + free) + limits.RESERVE_BLOCK_BYTES - 1
            ) // limits.RESERVE_BLOCK_BYTES
            rows = self._db.execute(
                "SELECT block_id FROM authority_reserve ORDER BY block_id LIMIT ?", (blocks,)
            ).fetchall()
            if len(rows) < blocks:
                raise StorageUnavailable("full")
            self._db.executemany("DELETE FROM authority_reserve WHERE block_id=?", rows)

    def delete(self, table: Table, key: str, *, expected_revision: int | None) -> bool:
        old = self.get(table, key)
        if old is None or (expected_revision is not None and old.revision != expected_revision):
            return False
        if table not in (Table.DRAFTS, Table.DRAFT_REVISIONS, Table.EVICTION_NOTICES):
            self._authority_space(0)
        self._charge(
            0, authority=table not in (Table.DRAFTS, Table.DRAFT_REVISIONS, Table.EVICTION_NOTICES)
        )
        self._db.execute(
            "DELETE FROM records WHERE table_name=? AND lookup=?",
            (table.value, self._lookup(table, key)),
        )
        if self._store._schema >= 3:
            self._db.execute(
                "DELETE FROM record_scopes WHERE table_name=? AND lookup=?",
                (table.value, self._lookup(table, key)),
            )
            if table is Table.DRAFT_REVISIONS:
                self._advance_epoch()
        return True

    def _notice(self, scope: tuple[str, ...], *, expired: int = 0, evicted: int = 0) -> None:
        key = _json(scope).decode()
        old = self.get(Table.EVICTION_NOTICES, key)
        if (
            old is None
            and self._db.execute(
                "SELECT count(*) FROM records WHERE table_name=?", (Table.EVICTION_NOTICES.value,)
            ).fetchone()[0]
            >= limits.MAX_NOTICE_ROWS
        ):
            raise StorageUnavailable("full")
        counts = json.loads(old.body) if old else {"expired": 0, "evicted": 0}
        counts["expired"] += expired
        counts["evicted"] += evicted
        notice = Record(
            Table.EVICTION_NOTICES,
            key,
            scope,
            _json(counts),
            old.revision + 1 if old else 1,
            self._store._now_ms(),
        )
        self._write(notice)
        self._notice_changes[scope] = _notice_value(notice.body)

    def _quota(self, incoming: Record, old: Record | None) -> None:
        if (
            len(incoming.body)
            > min(limits.MAX_DRAFT_BYTES, limits.MAX_DEVICE_BYTES, limits.MAX_TOTAL_BYTES)
            or limits.MAX_DRAFTS_PER_DEVICE < 1
        ):
            raise StorageUnavailable("full")
        device = self._store._hash("device", incoming.scope[:3])
        victims = 0
        while True:
            count, size = self._db.execute(
                "SELECT count(*), coalesce(sum(body_bytes),0) "
                "FROM records WHERE table_name=? AND device_hash=?",
                (Table.DRAFTS.value, device),
            ).fetchone()
            total = self._db.execute(
                "SELECT coalesce(sum(body_bytes),0) FROM records WHERE table_name=?",
                (Table.DRAFTS.value,),
            ).fetchone()[0]
            device_full = (
                count + (0 if old else 1) > limits.MAX_DRAFTS_PER_DEVICE
                or size - (len(old.body) if old else 0) + len(incoming.body)
                > limits.MAX_DEVICE_BYTES
            )
            total_full = (
                total - (len(old.body) if old else 0) + len(incoming.body) > limits.MAX_TOTAL_BYTES
            )
            if not device_full and not total_full:
                return
            if victims >= limits.MAX_SWEEP_ROWS:
                raise StorageUnavailable("full")
            query = "SELECT * FROM records WHERE table_name=? AND lookup<>?"
            args: list[object] = [Table.DRAFTS.value, self._lookup(Table.DRAFTS, incoming.key)]
            if device_full:
                query += " AND device_hash=?"
                args.append(device)
            row = self._db.execute(query + " ORDER BY accessed_ms, lookup LIMIT 1", args).fetchone()
            if row is None:
                raise StorageUnavailable("full")
            victim = self._decode(row)
            self._notice(victim.scope[:3], evicted=1)
            self.delete(victim.table, victim.key, expected_revision=victim.revision)
            victims += 1


class _DraftStore(CredentialStore):
    def __init__(self, store: PersistentStore, scope: tuple[str, ...]) -> None:
        self._store, self._scope = store, scope

    def _target(self, target: DraftTarget) -> tuple[str, tuple[str, ...]]:
        scope = (*self._scope, target.host, target.terminal)
        _validate_scope(scope)
        return _json(scope).decode(), scope

    @staticmethod
    def _legacy(key: str) -> DraftTarget:
        host, separator, terminal = key.removeprefix("draft-").partition(":")
        if not key.startswith("draft-") or not host or not separator or not terminal:
            raise StorageUnavailable("invalid_record")
        return DraftTarget(host, terminal)

    async def _run(self, operation: Callable[[], _Result], *, nonce_budget: int = 0) -> _Result:
        def work() -> _Result:
            try:
                # A save produces two rows; pruning must keep pace with unique
                # targets, rather than removing 128 after 256 new tombstones.
                threshold = min(limits.MAX_NONCE_HISTORY // 2, limits.MAX_SWEEP_ROWS * 2)
                if self._store._writes_since_maintenance >= threshold:
                    removed = self._store._prune_counters(self._store._now_ms())
                    self._store.maintain(force_drafts=bool(removed))
                self._store._preflight_drafts(nonce_budget)
                result = operation()
                notice = self.notice()
                self._store._notices[self._scope[:3]] = DraftNotice(
                    "evicted" if notice.evicted or notice.expired else "saved",
                    notice.evicted,
                    notice.expired,
                )
                return result
            except StorageUnavailable:
                notice = self.notice()
                self._store._notices[self._scope[:3]] = DraftNotice(
                    "unavailable", notice.evicted, notice.expired
                )
                raise

        return await self._store.run_draft(work)

    async def available(self) -> bool:
        def check() -> bool:
            status = self._store.status()
            if not status.available:
                raise StorageUnavailable(status.reason)
            return True

        try:
            return await self._run(check)
        except StorageUnavailable:
            return False

    async def load(self, target: DraftTarget) -> DraftRecord | None:
        return await self._run(lambda: self._load(target), nonce_budget=1)

    def _load(self, target: DraftTarget) -> DraftRecord | None:
        key, _ = self._target(target)
        with self._store.transaction() as tx:
            row = tx._db.execute(
                "SELECT * FROM records WHERE table_name=? AND lookup=?",
                (Table.DRAFTS.value, tx._lookup(Table.DRAFTS, key)),
            ).fetchone()
            record = tx._decode(row) if row is not None else None
            now = self._store._now_ms()
            if record is not None:
                if row["accessed_ms"] <= now - limits.DRAFT_IDLE_MS:
                    tx._notice(record.scope[:3], expired=1)
                    tx.delete(record.table, record.key, expected_revision=record.revision)
                    record = None
                else:
                    # Reads count as activity without changing the edit revision.
                    tx._write(record, accessed_ms=now)
        if record is None:
            return None
        return DraftRecord(target, record.body.decode(), record.revision, record.updated_ms)

    async def save(
        self, target: DraftTarget, snapshot: str | Mapping[str, Any], expected_revision: int | None
    ) -> DraftRecord:
        return await self._run(
            lambda: self._save(target, snapshot, expected_revision),
            nonce_budget=limits.MAX_SWEEP_ROWS + 2,
        )

    def _save(
        self, target: DraftTarget, snapshot: str | Mapping[str, Any], expected_revision: int | None
    ) -> DraftRecord:
        key, scope = self._target(target)
        text = snapshot if isinstance(snapshot, str) else _json(dict(snapshot)).decode()
        with self._store.transaction() as tx:
            revision = tx._next_revision()
            now = self._store._now_ms()
            if not tx.put(
                Record(Table.DRAFTS, key, scope, text.encode(), revision, now),
                expected_revision=expected_revision,
            ):
                raise StorageUnavailable("revision_conflict")
        return DraftRecord(target, text, revision, now)

    async def delete_revision(self, target: DraftTarget, revision: int) -> bool:
        return await self._run(lambda: self._delete_revision(target, revision))

    def _delete_revision(self, target: DraftTarget, revision: int) -> bool:
        key, _ = self._target(target)
        with self._store.transaction() as tx:
            return tx.delete(Table.DRAFTS, key, expected_revision=revision)

    async def get(self, key: str) -> str | None:
        record = await self.load(self._legacy(key))
        return record.snapshot if record else None

    async def set(self, key: str, value: str) -> None:
        await self._run(lambda: self._set(key, value), nonce_budget=limits.MAX_SWEEP_ROWS + 2)

    def _set(self, key: str, value: str) -> None:
        # Compatibility calls still perform an atomic revision increase, but cannot
        # supply the delivered-revision fence required by the new integration.
        target = self._legacy(key)
        storage_key, scope = self._target(target)
        with self._store.transaction() as tx:
            old = tx.get(Table.DRAFTS, storage_key)
            revision = tx._next_revision()
            if not tx.put(
                Record(
                    Table.DRAFTS,
                    storage_key,
                    scope,
                    value.encode(),
                    revision,
                    self._store._now_ms(),
                ),
                expected_revision=old.revision if old else None,
            ):
                raise StorageUnavailable("revision_conflict")

    async def delete(self, key: str) -> None:
        await self._run(lambda: self._delete(key))

    def _delete(self, key: str) -> None:
        storage_key, _ = self._target(self._legacy(key))
        with self._store.transaction() as tx:
            tx.delete(Table.DRAFTS, storage_key, expected_revision=None)

    async def clear_account(self) -> None:
        await self._run(self._clear_account)

    def _clear_account(self) -> None:
        # CredentialStore cleanup is device scoped, across this device's tabs only.
        device = self._store._hash("device", self._scope[:3])
        with self._store.transaction() as tx:
            rows = tx._db.execute(
                "SELECT * FROM records WHERE table_name=? AND device_hash=?",
                (Table.DRAFTS.value, device),
            ).fetchall()
            for row in rows:
                record = tx._decode(row)
                tx.delete(record.table, record.key, expected_revision=record.revision)

    def notice(self) -> DraftNotice:
        # Last completed operation/durable sweep counts, without event-loop I/O.
        return self._store._notices.get(self._scope[:3], DraftNotice())


def open_store(config: StorageConfig, provider: KeyProvider) -> PersistentStore:
    return PersistentStore(config, provider)
