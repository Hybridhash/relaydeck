"""Encrypted persistence content, private files, identity binding and quota failures."""

from __future__ import annotations

import asyncio
import os
import secrets
import sqlite3
import stat
from dataclasses import dataclass
from pathlib import Path

import pytest

from relaydeck import persistence_contracts as limits
from relaydeck.companion import persistence
from relaydeck.persistence_contracts import (
    DraftTarget,
    KeyUnavailable,
    Record,
    StorageUnavailable,
    Table,
)


@dataclass(frozen=True)
class Config:
    state_dir: Path


class MemoryKeys:
    def __init__(self) -> None:
        self.keys: dict[str, bytes] = {}
        self.locked = False

    def get(self, name: str, *, create: bool = False, allow_prompt: bool = False) -> bytes:
        assert not allow_prompt
        if self.locked or (name not in self.keys and not create):
            raise KeyUnavailable()
        return self.keys.setdefault(name, secrets.token_bytes(32))

    def retire(self, name: str) -> None:
        if self.locked:
            raise KeyUnavailable()
        self.keys.pop(name, None)

    def import_secret(
        self, name: str, material: limits.SecretMaterial, *, allow_prompt: bool = False
    ) -> limits.SecretMaterial:
        if self.locked:
            raise KeyUnavailable()
        old = self.keys.setdefault("material/" + name, material.value)
        if old != material.value:
            raise KeyUnavailable()
        return limits.SecretMaterial(old)

    def read_secret(self, name: str) -> limits.SecretMaterial:
        if self.locked or "material/" + name not in self.keys:
            raise KeyUnavailable()
        return limits.SecretMaterial(self.keys["material/" + name])

    def restore_generation(
        self, name: str, *, create: bool = False, allow_prompt: bool = False
    ) -> limits.RestoreGeneration:
        if self.locked or ("restore/" + name not in self.keys and not create):
            raise KeyUnavailable()
        raw = self.keys.setdefault("restore/" + name, bytes(8))
        return limits.RestoreGeneration(int.from_bytes(raw, "big"))

    def advance_restore_generation(self, name: str) -> limits.RestoreGeneration:
        old = self.restore_generation(name)
        value = old.value + 1
        self.keys["restore/" + name] = value.to_bytes(8, "big")
        return limits.RestoreGeneration(value)


def test_private_sidecars_and_unsafe_paths(tmp_path: Path) -> None:
    config = Config(tmp_path / "state")
    old = os.umask(0)
    try:
        store = persistence.open_store(config, MemoryKeys())
        with store.transaction() as tx:
            assert tx.put(
                Record(Table.REVOCATIONS, "device", ("account",), b"private", 1, 0),
                expected_revision=None,
            )
            for path in config.state_dir.iterdir():
                assert stat.S_IMODE(path.stat().st_mode) == 0o600
                assert path.stat().st_nlink == 1
            assert stat.S_IMODE(config.state_dir.stat().st_mode) == 0o700
        store.close()
    finally:
        os.umask(old)
    link = tmp_path / "alias"
    link.symlink_to(config.state_dir, target_is_directory=True)
    with pytest.raises(StorageUnavailable, match="unsafe_path"):
        persistence.open_store(Config(link), MemoryKeys())
    unsafe = tmp_path / "unsafe"
    unsafe.mkdir(mode=0o777)
    unsafe.chmod(0o777)
    with pytest.raises(StorageUnavailable, match="unsafe_path"):
        persistence.open_store(Config(unsafe / "nested"), MemoryKeys())


@pytest.mark.parametrize(
    "name",
    [
        "persistence.db",
        "persistence.db-wal",
        "persistence.db-shm",
        "persistence.db-journal",
        "persistence.db.lock",
    ],
)
@pytest.mark.parametrize("attack", ["symlink", "hardlink", "public"])
def test_unsafe_existing_files_rejected(tmp_path: Path, name: str, attack: str) -> None:
    state = tmp_path / "state"
    state.mkdir(mode=0o700)
    path = state / name
    other = tmp_path / "other"
    other.write_bytes(b"untouched")
    other.chmod(0o600)
    if attack == "symlink":
        path.symlink_to(other)
    elif attack == "hardlink":
        os.link(other, path)
    else:
        path.write_bytes(b"untouched")
        path.chmod(0o644)
    with pytest.raises(StorageUnavailable, match="unsafe_path"):
        persistence.open_store(Config(state), MemoryKeys())
    assert other.read_bytes() == b"untouched"


def test_aead_scope_tamper_and_nonce_uniqueness(tmp_path: Path) -> None:
    store = persistence.open_store(Config(tmp_path / "state"), MemoryKeys())
    with store.transaction() as tx:
        for index in range(20):
            assert tx.put(
                Record(Table.REVOCATIONS, str(index), ("account",), b"payload", 1, index),
                expected_revision=None,
            )
    rows = store._db.execute("SELECT nonce FROM records").fetchall()
    assert len({bytes(row[0]) for row in rows}) == 20
    store._db.execute(
        "UPDATE records SET scope_hash=? WHERE rowid=(SELECT min(rowid) FROM records)", ("0" * 64,)
    )
    with pytest.raises(StorageUnavailable, match="corrupt"):
        with store.transaction() as tx:
            tx.get(Table.REVOCATIONS, "0")
    assert not store.status().available
    store.close()


def test_canaries_absent_from_db_wal_and_logs(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    keys = MemoryKeys()
    store = persistence.open_store(Config(tmp_path / "state"), keys)
    token, draft, identity = "TOKEN_CANARY_83", "DRAFT_CANARY_29", "ACCOUNT_CANARY_47"
    with store.transaction() as tx:
        assert tx.put(
            Record(
                Table.WEB_SESSIONS,
                token,
                (identity, "device"),
                token.encode(),
                1,
                0,
                2_000_000_000_000,
            ),
            expected_revision=None,
        )
    drafts = store.scoped_drafts("origin", identity, "device", "tab")
    asyncio.run(drafts.save(DraftTarget("host", "terminal"), draft, None))
    store.sweep(1)
    for path in (tmp_path / "state").iterdir():
        raw = path.read_bytes()
        assert all(canary.encode() not in raw for canary in (token, draft, identity))
    assert all(canary not in caplog.text for canary in (token, draft, identity))
    store.close()


def test_lock_after_begin_prevents_commit_and_later_restores(tmp_path: Path) -> None:
    keys = MemoryKeys()
    store = persistence.open_store(Config(tmp_path / "state"), keys)
    with pytest.raises(StorageUnavailable, match="keys_unavailable"):
        with store.transaction() as tx:
            tx.put(
                Record(Table.REVOCATIONS, "device", ("account",), b"private", 1, 0),
                expected_revision=None,
            )
            keys.locked = True
    keys.locked = False
    with store.transaction() as tx:
        assert tx.get(Table.REVOCATIONS, "device") is None
    store.close()


def test_device_origin_and_account_isolation(tmp_path: Path) -> None:
    store = persistence.open_store(Config(tmp_path / "state"), MemoryKeys())
    target = DraftTarget("host", "terminal")

    async def scenario() -> None:
        own = store.scoped_drafts("origin", "account", "device", "tab")
        await own.save(target, "own", None)
        for scope in [
            ("other", "account", "device", "tab"),
            ("origin", "other", "device", "tab"),
            ("origin", "account", "other", "tab"),
        ]:
            foreign = store.scoped_drafts(*scope)
            assert await foreign.load(target) is None
            assert not await foreign.delete_revision(target, 1)
            await foreign.clear_account()
        assert await own.get("draft-host:terminal") == "own"

    asyncio.run(scenario())
    store.close()


def test_oversize_and_unfulfillable_quota_leave_existing_draft(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = persistence.open_store(Config(tmp_path / "state"), MemoryKeys())
    drafts = store.scoped_drafts("origin", "account", "device", "tab")
    target = DraftTarget("host", "terminal")

    async def scenario() -> None:
        original = await drafts.save(target, "kept", None)
        with pytest.raises(StorageUnavailable, match="full"):
            await drafts.save(target, "x" * (limits.MAX_DRAFT_BYTES + 1), original.revision)
        monkeypatch.setattr(limits, "MAX_DEVICE_BYTES", 3)
        with pytest.raises(StorageUnavailable, match="full"):
            await drafts.save(target, "new text", original.revision)
        monkeypatch.setattr(limits, "MAX_DEVICE_BYTES", 8 * 1024 * 1024)
        assert (await drafts.load(target)).snapshot == "kept"  # type: ignore[union-attr]

    asyncio.run(scenario())
    store.close()


def test_unknown_schema_and_missing_keys_preserve_database(tmp_path: Path) -> None:
    keys, config = MemoryKeys(), Config(tmp_path / "state")
    store = persistence.open_store(config, keys)
    store.close()
    with sqlite3.connect(config.state_dir / "persistence.db") as db:
        db.execute("PRAGMA user_version=999")
    with pytest.raises(StorageUnavailable, match="unknown_schema"):
        persistence.open_store(config, keys)
    with sqlite3.connect(config.state_dir / "persistence.db") as db:
        assert db.execute("PRAGMA user_version").fetchone()[0] == 999


def test_missing_existing_key_never_reprovisions_it(tmp_path: Path) -> None:
    keys, config = MemoryKeys(), Config(tmp_path / "state")
    store = persistence.open_store(config, keys)
    with store.transaction() as tx:
        tx.put(
            Record(Table.REVOCATIONS, "device", ("account",), b"retained", 1, 0),
            expected_revision=None,
        )
    active_key = store._key_ids["authority"]
    store.close()
    before = (config.state_dir / "persistence.db").read_bytes()
    key = keys.keys.pop(active_key)
    with pytest.raises(StorageUnavailable, match="keys_unavailable"):
        persistence.open_store(config, keys)
    assert active_key not in keys.keys
    assert (config.state_dir / "persistence.db").read_bytes() == before
    keys.keys[active_key] = key
    restored = persistence.open_store(config, keys)
    with restored.transaction() as tx:
        assert tx.get(Table.REVOCATIONS, "device") is not None
    restored.close()


def test_schema_and_key_metadata_changes_while_open_fail_closed(tmp_path: Path) -> None:
    store = persistence.open_store(Config(tmp_path / "state"), MemoryKeys())
    store._db.execute("PRAGMA user_version=999")
    assert store.status().reason == "unknown_schema"
    store.close()
    other = persistence.open_store(Config(tmp_path / "other"), MemoryKeys())
    other._db.execute("UPDATE metadata SET key_ids='{}'")
    assert other.status().reason == "corrupt"
    other.close()


@pytest.mark.parametrize(
    "column,value",
    [
        ("revision", 2),
        ("expires_ms", 9),
        ("record_version", 2),
        ("body_bytes", 999),
        ("accessed_ms", 8),
        ("device_hash", "0" * 64),
        ("ciphertext", b"untrusted"),
    ],
)
def test_authenticated_metadata_and_payload_tamper_fails_closed(
    tmp_path: Path,
    column: str,
    value: object,
) -> None:
    store = persistence.open_store(Config(tmp_path / "state"), MemoryKeys())
    with store.transaction() as tx:
        tx.put(
            Record(Table.REVOCATIONS, "device", ("account",), b"retained", 1, 0),
            expected_revision=None,
        )
    # Column names come exclusively from this test's fixed parameter list.
    store._db.execute(f"UPDATE records SET {column}=?", (value,))
    with pytest.raises(StorageUnavailable, match="corrupt"):
        with store.transaction() as tx:
            tx.get(Table.REVOCATIONS, "device")
    assert store.status().reason == "corrupt"
    store.close()


def test_caught_quota_error_cannot_commit_partial_eviction(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = persistence.open_store(Config(tmp_path / "state"), MemoryKeys())
    drafts = store.scoped_drafts("origin", "account", "device", "tab")
    target = DraftTarget("host", "kept")
    first = asyncio.run(drafts.save(target, "retained", None))
    monkeypatch.setattr(limits, "MAX_DRAFTS_PER_DEVICE", 1)
    monkeypatch.setattr(limits, "MAX_NOTICE_ROWS", 0)
    with store.transaction() as tx:
        key, scope = drafts._target(DraftTarget("host", "new"))  # type: ignore[attr-defined]
        with pytest.raises(StorageUnavailable, match="full"):
            tx.put(Record(Table.DRAFTS, key, scope, b"new", 1, 2), expected_revision=None)
        assert tx.get(Table.DRAFTS, key) is None
    assert asyncio.run(drafts.load(target)) == first
    assert drafts.notice().evicted == 0
    store.close()


def test_nonce_collision_never_overwrites_or_reuses_committed_nonce(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = persistence.open_store(Config(tmp_path / "state"), MemoryKeys())
    fixed = b"n" * 12
    monkeypatch.setattr(secrets, "token_bytes", lambda n: fixed)
    with store.transaction() as tx:
        tx.put(
            Record(Table.REVOCATIONS, "first", ("account",), b"first", 1, 0), expected_revision=None
        )
    with pytest.raises(StorageUnavailable, match="full"):
        with store.transaction() as tx:
            tx.put(
                Record(Table.REVOCATIONS, "second", ("account",), b"second", 1, 0),
                expected_revision=None,
            )
    with store.transaction() as tx:
        assert tx.get(Table.REVOCATIONS, "first") is not None
        assert tx.get(Table.REVOCATIONS, "second") is None
    assert store._db.execute("SELECT count(*) FROM nonces").fetchone()[0] == 1
    store.close()


def test_draft_full_preserves_authority_capacity(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = persistence.open_store(Config(tmp_path / "state"), MemoryKeys())
    drafts = store.scoped_drafts("origin", "account", "device", "tab")
    target = DraftTarget("host", "terminal")
    monkeypatch.setattr(limits, "MAX_TOTAL_BYTES", 4)
    asyncio.run(drafts.save(target, "kept", None))
    # Filling the payload cap must not consume reserved authority space.
    with store.transaction() as tx:
        assert tx.put(
            Record(Table.REVOCATIONS, "logout", ("account",), b"revoked", 1, 1),
            expected_revision=None,
        )
    with store.transaction() as tx:
        assert tx.get(Table.REVOCATIONS, "logout") is not None
    store.close()


def test_physical_page_full_keeps_revocation_and_logout_writable(tmp_path: Path) -> None:
    store = persistence.open_store(Config(tmp_path / "state"), MemoryKeys())
    with store.transaction() as tx:
        tx.put(
            Record(
                Table.WEB_SESSIONS,
                "logout-token",
                ("origin", "account", "device"),
                b"old",
                1,
                1,
                2_000_000_000_000,
            ),
            expected_revision=None,
        )
    pages = store._db.execute("PRAGMA page_count").fetchone()[0]
    store._db.execute(f"PRAGMA max_page_count={pages}")
    drafts = store.scoped_drafts("origin", "account", "device", "tab")
    with pytest.raises(StorageUnavailable, match="full"):
        asyncio.run(
            drafts.save(DraftTarget("host", "terminal"), "x" * limits.MAX_DRAFT_BYTES, None)
        )
    with store.transaction() as tx:
        assert tx.put(
            Record(Table.REVOCATIONS, "logout", ("account",), b"x" * 65536, 1, 1),
            expected_revision=None,
        )
        assert tx.delete(Table.WEB_SESSIONS, "logout-token", expected_revision=1)
    with store.transaction() as tx:
        assert tx.get(Table.REVOCATIONS, "logout") is not None
        assert tx.get(Table.WEB_SESSIONS, "logout-token") is None
    store.close()


def test_no_free_disk_protects_reserve_for_revocation_and_logout(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import shutil

    store = persistence.open_store(Config(tmp_path / "state"), MemoryKeys())
    with store.transaction() as tx:
        tx.put(
            Record(Table.WEB_SESSIONS, "token", ("account",), b"old", 1, 1, 2_000_000_000_000),
            expected_revision=None,
        )
    reserve = tmp_path / "state/persistence.reserve"
    before = reserve.stat().st_size
    usage = shutil.disk_usage(tmp_path)
    monkeypatch.setattr(shutil, "disk_usage", lambda path: usage._replace(free=0))
    drafts = store.scoped_drafts("origin", "account", "device", "tab")
    with pytest.raises(StorageUnavailable, match="full"):
        asyncio.run(drafts.save(DraftTarget("host", "terminal"), "new", None))
    assert reserve.stat().st_size == before
    with store.transaction() as tx:
        assert tx.put(
            Record(Table.REVOCATIONS, "logout", ("account",), b"revoked", 1, 1),
            expected_revision=None,
        )
        assert tx.delete(Table.WEB_SESSIONS, "token", expected_revision=1)
    assert reserve.stat().st_size < before
    assert stat.S_IMODE(reserve.stat().st_mode) == 0o600
    with store.transaction() as tx:
        assert tx.get(Table.REVOCATIONS, "logout") is not None
        assert tx.get(Table.WEB_SESSIONS, "token") is None
    store.close()


def test_draft_wal_budget_leaves_authority_headroom(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = persistence.open_store(Config(tmp_path / "state"), MemoryKeys())
    with store.transaction() as tx:
        tx.put(
            Record(Table.WEB_SESSIONS, "token", ("account",), b"old", 1, 1, 2_000_000_000_000),
            expected_revision=None,
        )
    wal = (tmp_path / "state/persistence.db-wal").stat().st_size
    monkeypatch.setattr(limits, "MAX_WAL_BYTES", 2 * (wal + 16384))
    drafts = store.scoped_drafts("origin", "account", "device", "tab")
    with pytest.raises(StorageUnavailable, match="full"):
        asyncio.run(drafts.save(DraftTarget("host", "terminal"), "new", None))
    with store.transaction() as tx:
        assert tx.put(
            Record(Table.REVOCATIONS, "logout", ("account",), b"revoked", 1, 1),
            expected_revision=None,
        )
        assert tx.delete(Table.WEB_SESSIONS, "token", expected_revision=1)
    with store.transaction() as tx:
        assert tx.get(Table.REVOCATIONS, "logout") is not None
        assert tx.get(Table.WEB_SESSIONS, "token") is None
    store.close()
