"""Private persistence transactions, draft revisions, retention and Keychain opt-in."""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import secrets
import sqlite3
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any, cast

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
from tests.support.persistence.persistence import Config, MemoryKeys, record


def test_locked_or_corrupt_never_falls_back(tmp_path: Path) -> None:
    keys = MemoryKeys()
    store = persistence.open_store(Config(tmp_path / "state"), keys)
    with store.transaction() as tx:
        assert tx.put(record(), expected_revision=None)
    keys.locked = True
    assert not store.status().available
    with pytest.raises(StorageUnavailable, match="keys_unavailable"):
        with store.transaction() as tx:
            tx.get(Table.WEB_SESSIONS, "token")
    keys.locked = False
    assert store.status().available
    with store.transaction() as tx:
        assert tx.get(Table.WEB_SESSIONS, "token") == record()
    assert not any(keys.prompts)
    store.close()
    path = tmp_path / "state/persistence.db"
    path.write_bytes(b"corrupt ciphertext canary")
    with pytest.raises(StorageUnavailable, match="corrupt"):
        persistence.open_store(Config(path.parent), keys)
    assert path.read_bytes() == b"corrupt ciphertext canary"


def test_transaction_rollback_cas_and_restart(tmp_path: Path) -> None:
    keys, config = MemoryKeys(), Config(tmp_path / "state")
    store = persistence.open_store(config, keys)
    with pytest.raises(RuntimeError, match="abort"):
        with store.transaction() as tx:
            assert tx.put(record(), expected_revision=None)
            raise RuntimeError("abort")
    with store.transaction() as tx:
        assert tx.get(Table.WEB_SESSIONS, "token") is None
        assert tx.put(record(), expected_revision=None)
        assert not tx.put(record(), expected_revision=None)
        newer = Record(
            Table.WEB_SESSIONS, "token", record().scope, b"replacement", 2, 2, record().expires_ms
        )
        assert not tx.put(newer, expected_revision=99)
        assert tx.put(newer, expected_revision=1)
        assert not tx.delete(Table.WEB_SESSIONS, "token", expected_revision=1)
    store.close()
    reopened = persistence.open_store(config, keys)
    with reopened.transaction() as tx:
        assert tx.get(Table.WEB_SESSIONS, "token") == newer
        assert tx.delete(Table.WEB_SESSIONS, "token", expected_revision=2)
    reopened.close()


def test_migration_crash_rolls_back(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    keys, config = MemoryKeys(), Config(tmp_path / "state")
    with monkeypatch.context() as patch:
        patch.setattr(persistence, "SCHEMA_VERSION", 1)
        store = persistence.open_store(config, keys)
        with store.transaction() as tx:
            assert tx.put(record(), expected_revision=None)
        store.close()

    def crash(db: sqlite3.Connection) -> None:
        db.execute("CREATE TABLE interrupted (n INTEGER)")
        raise RuntimeError("migration failed")

    with monkeypatch.context() as patch:
        patch.setitem(persistence._MIGRATIONS, 2, crash)
        with pytest.raises(StorageUnavailable):
            persistence.open_store(config, keys)
    with sqlite3.connect(config.state_dir / "persistence.db") as db:
        assert db.execute("PRAGMA user_version").fetchone()[0] == 1
        assert (
            db.execute("SELECT name FROM sqlite_master WHERE name='interrupted'").fetchone() is None
        )
    reopened = persistence.open_store(config, keys)
    with reopened.transaction() as tx:
        assert tx.get(Table.WEB_SESSIONS, "token") == record()
    reopened.close()


def test_sweep_bounds_and_durable_notice(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(limits, "MAX_DRAFTS_PER_DEVICE", 2)
    monkeypatch.setattr(limits, "MAX_SWEEP_ROWS", 1)
    keys, config = MemoryKeys(), Config(tmp_path / "state")
    store = persistence.open_store(config, keys)
    clock = [10_000]
    monkeypatch.setattr(store, "_now_ms", lambda: clock[0])
    drafts = store.scoped_drafts("origin", "account", "device", "tab")

    async def scenario() -> None:
        for index in range(3):
            clock[0] += 1
            await drafts.save(DraftTarget("host", str(index)), f"draft-{index}", None)
        assert await drafts.load(DraftTarget("host", "0")) is None
        assert drafts.notice().evicted == 1

    asyncio.run(scenario())
    report = store.sweep(clock[0] + limits.DRAFT_IDLE_MS + 1)
    assert report.expired == 1 and report.more
    assert drafts.notice().expired == 1
    store.close()
    reopened = persistence.open_store(config, keys)
    notice = reopened.scoped_drafts("origin", "account", "device", "other-tab").notice()
    assert notice.evicted == 1 and notice.expired == 2
    reopened.close()


def test_revisioned_drafts_isolate_tabs_and_delete_only_delivered_revision(tmp_path: Path) -> None:
    keys = MemoryKeys()
    store = persistence.open_store(Config(tmp_path / "state"), keys)
    a = store.scoped_drafts("origin", "account", "device", "tab-a")
    b = store.scoped_drafts("origin", "account", "device", "tab-b")
    target = DraftTarget("host", "agent")

    async def scenario() -> None:
        first = await a.save(target, {"draft": "same text", "pictures": []}, None)
        assert await b.load(target) is None
        second = await a.save(target, {"draft": "same text", "pictures": []}, first.revision)
        assert second.revision > first.revision
        assert not await a.delete_revision(target, first.revision)
        assert (await a.load(target)) == second
        with pytest.raises(StorageUnavailable, match="revision_conflict"):
            await a.save(target, "stale", first.revision)
        assert await a.delete_revision(target, second.revision)
        third = await a.save(target, "same text", None)
        assert third.revision > second.revision
        assert not await a.delete_revision(target, first.revision)
        await a.set("draft-host:other", "raw snapshot")
        assert await a.get("draft-host:other") == "raw snapshot"
        await a.delete("draft-host:other")
        assert await a.get("draft-host:other") is None
        await b.save(target, "other tab", None)
        await a.clear_account()
        assert await b.load(target) is None

    asyncio.run(scenario())
    store.close()


def test_expiry_is_absolute_and_uncertain_fences_never_sweep(tmp_path: Path) -> None:
    store = persistence.open_store(Config(tmp_path / "state"), MemoryKeys())
    with store.transaction() as tx:
        assert tx.put(record(expiry=1), expected_revision=None)
        assert tx.put(
            Record(Table.PENDING_FENCES, "command", ("device",), b"unknown", 1, 0),
            expected_revision=None,
        )
        assert tx.put(
            Record(Table.REVOCATIONS, "revoked", ("device",), b"revoked", 1, 0),
            expected_revision=None,
        )
    assert store.sweep(2).expired == 1
    with store.transaction() as tx:
        assert tx.get(Table.WEB_SESSIONS, "token") is None
        assert tx.get(Table.PENDING_FENCES, "command") is not None
        assert tx.get(Table.REVOCATIONS, "revoked") is not None
    store.close()


def test_key_rotation_keeps_old_keys_and_reads_after_restart(tmp_path: Path) -> None:
    keys, config = MemoryKeys(), Config(tmp_path / "state")
    store = persistence.open_store(config, keys)
    with store.transaction() as tx:
        assert tx.put(record(), expected_revision=None)
    old = dict(keys.keys)
    store.rotate_keys()
    assert all(keys.keys[name] == value for name, value in old.items())
    store.close()
    restored = persistence.open_store(config, keys)
    with restored.transaction() as tx:
        assert tx.get(Table.WEB_SESSIONS, "token") == record()
    restored.close()


def test_rotation_failure_keeps_previous_generation_and_records(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    keys, config = MemoryKeys(), Config(tmp_path / "state")
    store = persistence.open_store(config, keys)
    with store.transaction() as tx:
        tx.put(record(), expected_revision=None)
        tx.put(record("second"), expected_revision=None)
    old_ids = dict(store._key_ids)
    original = persistence.Transaction._write
    calls = [0]

    def fail(
        tx: persistence.Transaction,
        value: Record,
        *,
        key_ids: dict[str, str] | None = None,
        keys: dict[str, bytes] | None = None,
        accessed_ms: int | None = None,
    ) -> None:
        calls[0] += 1
        original(tx, value, key_ids=key_ids, keys=keys, accessed_ms=accessed_ms)
        if calls[0] == 1:
            raise RuntimeError("rotation interrupted")

    with monkeypatch.context() as patch:
        patch.setattr(persistence.Transaction, "_write", fail)
        with pytest.raises(RuntimeError, match="rotation interrupted"):
            store.rotate_keys()
    assert store._key_ids == old_ids
    store.close()
    reopened = persistence.open_store(config, keys)
    with reopened.transaction() as tx:
        assert tx.get(Table.WEB_SESSIONS, "token") == record()
        assert tx.get(Table.WEB_SESSIONS, "second") == record("second")
    reopened.close()


def test_rotation_key_loss_before_commit_keeps_old_generation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    keys, config = MemoryKeys(), Config(tmp_path / "state")
    store = persistence.open_store(config, keys)
    with store.transaction() as tx:
        tx.put(record(), expected_revision=None)
    old_ids = dict(store._key_ids)
    old_names = set(keys.keys)
    reads: dict[str, int] = {}
    original_get = keys.get

    def fail_new(name: str, *, create: bool = False, allow_prompt: bool = False) -> bytes:
        if not create and name not in old_names:
            reads[name] = reads.get(name, 0) + 1
            if reads[name] == 2:
                keys.keys.pop(name)
        return original_get(name, create=create, allow_prompt=allow_prompt)

    with monkeypatch.context() as patch:
        patch.setattr(keys, "get", fail_new)
        with pytest.raises(StorageUnavailable, match="keys_unavailable"):
            store.rotate_keys()
    assert store._key_ids == old_ids and store.status().available
    store.close()
    restored = persistence.open_store(config, keys)
    with restored.transaction() as tx:
        assert tx.get(Table.WEB_SESSIONS, "token") == record()
    restored.close()


def test_reads_extend_only_draft_idle_not_revision_or_auth_deadline(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = persistence.open_store(Config(tmp_path / "state"), MemoryKeys())
    clock = [100]
    monkeypatch.setattr(store, "_now_ms", lambda: clock[0])
    drafts = store.scoped_drafts("origin", "account", "device", "tab")
    target = DraftTarget("host", "terminal")
    first = asyncio.run(drafts.save(target, "unchanged", None))
    with store.transaction() as tx:
        tx.put(record(expiry=1000), expected_revision=None)
    clock[0] += limits.DRAFT_IDLE_MS - 1
    assert asyncio.run(drafts.load(target)) == first
    assert store.sweep(clock[0] + 2).expired == 1  # Auth expires; draft read is activity.
    assert asyncio.run(drafts.load(target)) == first
    clock[0] += limits.DRAFT_IDLE_MS + 1
    assert store.sweep(clock[0]).expired == 1
    assert asyncio.run(drafts.load(target)) is None
    store.close()


def test_writers_wait_bounded_and_transaction_handle_expires(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(limits, "BUSY_TIMEOUT_MS", 30)
    keys, config = MemoryKeys(), Config(tmp_path / "state")
    store = persistence.open_store(config, keys)
    with pytest.raises(StorageUnavailable, match="busy"):
        persistence.open_store(config, keys)
    entered, done = threading.Event(), threading.Event()

    def hold() -> None:
        with store.transaction():
            entered.set()
            assert done.wait(2)

    thread = threading.Thread(target=hold)
    thread.start()
    assert entered.wait(1)
    try:
        start = time.monotonic()
        assert store.status().reason == "busy"
        assert time.monotonic() - start < 1
    finally:
        done.set()
        thread.join(2)
    with store.transaction() as expired:
        expired.put(record(), expected_revision=None)
    with pytest.raises(StorageUnavailable, match="closed"):
        expired.get(Table.WEB_SESSIONS, "token")
    store.close()
    assert store.status().reason == "closed"


def test_locked_fresh_setup_does_not_create_empty_database(tmp_path: Path) -> None:
    keys, config = MemoryKeys(), Config(tmp_path / "state")
    keys.locked = True
    with pytest.raises(StorageUnavailable, match="keys_unavailable"):
        persistence.open_store(config, keys)
    assert not (config.state_dir / "persistence.db").exists()
    keys.locked = False
    store = persistence.open_store(config, keys)
    assert store.status().available
    store.close()


def test_external_sqlite_writer_reports_busy_not_corruption(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(limits, "BUSY_TIMEOUT_MS", 20)
    keys, config = MemoryKeys(), Config(tmp_path / "state")
    store = persistence.open_store(config, keys)
    store.close()
    external = sqlite3.connect(config.state_dir / "persistence.db", isolation_level=None)
    external.execute("BEGIN IMMEDIATE")
    try:
        with pytest.raises(StorageUnavailable, match="busy"):
            persistence.open_store(config, keys)
    finally:
        external.execute("ROLLBACK")
        external.close()
    reopened = persistence.open_store(config, keys)
    assert reopened.status().available
    reopened.close()


def test_expired_draft_read_cannot_revive_it_without_a_sweep(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = persistence.open_store(Config(tmp_path / "state"), MemoryKeys())
    clock = [100]
    monkeypatch.setattr(store, "_now_ms", lambda: clock[0])
    drafts = store.scoped_drafts("origin", "account", "device", "tab")
    target = DraftTarget("host", "terminal")
    first = asyncio.run(drafts.save(target, "expired", None))
    clock[0] += limits.DRAFT_IDLE_MS
    assert asyncio.run(drafts.load(target)) is None
    assert drafts.notice().expired == 1
    assert not asyncio.run(drafts.delete_revision(target, first.revision))
    fresh = asyncio.run(drafts.save(target, "new", None))
    assert fresh.revision > first.revision
    store.close()


def test_keychain_default_never_reads_locked_keys_and_restores_ui_flag(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import ctypes as ct

    from relaydeck.companion import credentials

    released: list[int] = []
    ui: list[bool] = []
    reads: list[bool] = []
    unlocked = [False]

    class Security:
        def SecKeychainGetUserInteractionAllowed(self, pointer: Any) -> int:
            ct.cast(pointer, ct.POINTER(ct.c_ubyte)).contents.value = 1
            return 0

        def SecKeychainSetUserInteractionAllowed(self, state: bool) -> int:
            ui.append(bool(state))
            return 0

        def SecKeychainCopyDefault(self, pointer: Any) -> int:
            ct.cast(pointer, ct.POINTER(ct.c_void_p)).contents.value = 123
            return 0

        def SecKeychainGetStatus(self, chain: object, pointer: Any) -> int:
            ct.cast(pointer, ct.POINTER(ct.c_uint32)).contents.value = int(unlocked[0])
            return 0

    class CoreFoundation:
        def CFRelease(self, reference: ct.c_void_p) -> None:
            released.append(reference.value or 0)

    backend = credentials._Keychain.__new__(credentials._Keychain)
    monkeypatch.setattr(backend, "sec", Security(), raising=False)
    monkeypatch.setattr(backend, "cf", CoreFoundation(), raising=False)

    def read(name: str, chain: ct.c_void_p, allow_prompt: bool) -> bytes:
        reads.append(allow_prompt)
        return b"k" * 32

    monkeypatch.setattr(backend, "_read", read)
    provider = credentials.MacKeyProvider.__new__(credentials.MacKeyProvider)
    provider._backend = backend
    with pytest.raises(KeyUnavailable):
        provider.get("probe")
    assert reads == [] and ui == [False, True] and released == [123]
    unlocked[0] = True
    assert provider.get("probe") == b"k" * 32
    unlocked[0] = False
    with pytest.raises(KeyUnavailable):
        provider.get("probe")  # No previously fetched key may bypass lock.
    assert reads == [False]
    assert provider.get("probe", allow_prompt=True) == b"k" * 32
    assert reads == [False, True] and ui == [False, True] * 3


def test_keychain_query_explicitly_fails_ui_and_scopes_to_default_keychain(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import ctypes as ct

    from relaydeck.companion import credentials

    constants: dict[str, int] = {}
    queries: list[dict[int, int]] = []
    released: list[int] = []
    references = [100]

    def constant(name: str) -> int:
        return constants.setdefault(name, 1000 + len(constants))

    def reference() -> int:
        references[0] += 1
        return references[0]

    class CoreFoundation:
        def CFStringCreateWithCString(self, allocator: object, value: bytes, encoding: int) -> int:
            assert encoding == 0x08000100
            return reference()

        def CFArrayCreate(
            self, allocator: object, values: Any, count: int, callbacks: object
        ) -> int:
            assert count == 1 and values[0] == 123
            return reference()

        def CFDictionaryCreate(
            self,
            allocator: object,
            keys: Any,
            values: Any,
            count: int,
            key_callbacks: object,
            value_callbacks: object,
        ) -> int:
            queries.append({keys[i]: values[i] for i in range(count)})
            return reference()

        def CFRelease(self, value: int) -> None:
            released.append(value)

    backend = credentials._Keychain.__new__(credentials._Keychain)
    backend.service = "dev.relaydeck.persistence.test"
    monkeypatch.setattr(backend, "cf", CoreFoundation(), raising=False)
    monkeypatch.setattr(backend, "_constant", constant)
    backend.key_callbacks = credentials._KeyCallbacks()
    backend.value_callbacks = credentials._Callbacks()
    backend.array_callbacks = credentials._Callbacks()
    with backend._query("probe", ct.c_void_p(123), allow_prompt=False, reading=True):
        query = queries[-1]
        assert query[constant("kSecUseAuthenticationUI")] == constant("kSecUseAuthenticationUIFail")
        assert query[constant("kSecMatchLimit")] == constant("kSecMatchLimitOne")
        assert query[constant("kSecReturnData")] == constant("kCFBooleanTrue")
        assert constant("kSecMatchSearchList") in query
    assert len(released) == 4 and len(set(released)) == 4


@pytest.mark.skipif(
    sys.platform != "darwin" or os.environ.get("RELAYDECK_KEYCHAIN_TEST") != "1",
    reason="explicit real Keychain opt-in only",
)
def test_real_keychain_throwaway_item() -> None:
    from relaydeck.companion.credentials import MacKeyProvider

    service = "dev.relaydeck.persistence.test." + secrets.token_hex(16)
    provider = MacKeyProvider(service=service)
    try:
        input("Lock the DEFAULT Keychain in Keychain Access, then press Enter: ")
        assert provider._backend.is_locked(), "default Keychain must be locked for this proof"
        with pytest.raises(KeyUnavailable):
            provider.get("probe", create=True)
        input("Next, CANCEL the Keychain setup dialog. Press Enter to start: ")
        with pytest.raises(KeyUnavailable):
            provider.get("probe", create=True, allow_prompt=True)
        input("Next, APPROVE the one-time setup/unlock dialog. Press Enter to start: ")
        key = provider.get("probe", create=True, allow_prompt=True)
        assert len(key) == 32
        assert provider._backend.trusts_current_application("probe")
        assert provider.get("probe") == key
        assert MacKeyProvider(service=service).get("probe") == key
        # Same companion interpreter, new process; no secret in argv/stdout.
        probe = (
            "import hashlib,sys; from relaydeck.companion.credentials import MacKeyProvider; "
            "key=MacKeyProvider(service=sys.argv[1]).get('probe'); "
            "sys.exit(0 if hashlib.sha256(key).digest()==sys.stdin.buffer.read() else 1)"
        )
        result = subprocess.run(
            [sys.executable, "-B", "-c", probe, service],
            input=hashlib.sha256(key).digest(),
            capture_output=True,
            timeout=30,
            check=False,
        )
        assert result.returncode == 0 and not result.stdout and not result.stderr
        input("Lock the DEFAULT Keychain again, then press Enter: ")
        assert provider._backend.is_locked()
        with pytest.raises(KeyUnavailable):
            provider.get("probe")
        with pytest.raises(KeyUnavailable):
            provider.get("probe", create=True)
    finally:
        input("Unlock the DEFAULT Keychain for throwaway-item cleanup, then press Enter: ")
        provider.delete("probe", allow_prompt=True)


# Further regression contracts. Runtime wiring is out of scope here.
def test_churn_reclaims_retired_nonces_and_generation_fenced_tombstones(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    keys = MemoryKeys()
    clock = [1_900_000_000_000]
    monkeypatch.setattr(persistence.PersistentStore, "_now_ms", lambda self: clock[0])
    store = persistence.open_store(Config(tmp_path / "state"), keys)
    drafts = store.scoped_drafts("origin", "account", "device", "tab")
    target = DraftTarget("host", "same")

    async def churn() -> int:
        first = await drafts.save(target, "same", None)
        assert await drafts.delete_revision(target, first.revision)
        for index in range(200):
            t = DraftTarget("host", str(index % 100))
            saved = await drafts.save(t, "churn", None)
            assert await drafts.delete_revision(t, saved.revision)
        return first.revision

    stale_revision = asyncio.run(churn())
    old_key_ids = {store._key_ids["drafts"]}
    clock[0] += 8 * 24 * 60 * 60 * 1000
    store.sweep(clock[0])
    assert (
        store._db.execute(
            "SELECT count(*) FROM records WHERE table_name='draft_revisions'"
        ).fetchone()[0]
        == 0
    )
    assert store._db.execute("SELECT count(*) FROM nonces").fetchone()[0] < 128
    assert not old_key_ids.intersection(keys.keys)
    saved = asyncio.run(drafts.save(target, "same", None))
    assert saved.revision != stale_revision
    assert not asyncio.run(drafts.delete_revision(target, stale_revision))
    store.close()


def test_retirement_failure_keeps_nonce_history(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    keys = MemoryKeys()
    store = persistence.open_store(Config(tmp_path / "state"), keys)
    with store.transaction() as tx:
        tx.put(record(), expected_revision=None)
    old_ids = set(store._key_ids.values())
    store.rotate_keys()
    before = store._db.execute("SELECT count(*) FROM nonces").fetchone()[0]

    def fail(name: str) -> None:
        raise KeyUnavailable()

    monkeypatch.setattr(keys, "retire", fail)
    with pytest.raises(StorageUnavailable, match="keys_unavailable"):
        store.maintain()
    assert store._db.execute("SELECT count(*) FROM nonces").fetchone()[0] == before
    assert old_ids.issubset(keys.keys)
    store.close()


def test_async_drafts_keep_loop_responsive_under_contention(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(limits, "BUSY_TIMEOUT_MS", 120)
    store = persistence.open_store(Config(tmp_path / "state"), MemoryKeys())
    drafts = store.scoped_drafts("origin", "account", "device", "tab")
    entered, release = threading.Event(), threading.Event()

    def hold() -> None:
        with store.transaction():
            entered.set()
            assert release.wait(2)

    thread = threading.Thread(target=hold)
    thread.start()
    assert entered.wait(1)

    async def exercise() -> None:
        start = time.monotonic()
        times: list[float] = []
        asyncio.get_running_loop().call_later(0.01, lambda: times.append(time.monotonic() - start))
        assert not await drafts.available()
        await asyncio.sleep(0)
        assert times and times[0] < 0.06

    try:
        asyncio.run(exercise())
    finally:
        release.set()
        thread.join(2)
        store.close()


def test_scoped_scan_is_bounded_and_revoke_needs_no_private_sql(tmp_path: Path) -> None:
    store = persistence.open_store(Config(tmp_path / "state"), MemoryKeys())
    with store.transaction() as tx:
        for index in range(5):
            tx.put(record(str(index)), expected_revision=None)
        tx.put(
            Record(
                Table.WEB_SESSIONS,
                "foreign",
                ("origin", "other", "device"),
                b"foreign",
                1,
                1,
                2_000_000_000_000,
            ),
            expected_revision=None,
        )
        page = tx.scan(Table.WEB_SESSIONS, scope=("origin", "account"), limit=2)
        seen = list(page.records)
        while page.next_cursor:
            page = tx.scan(
                Table.WEB_SESSIONS, scope=("origin", "account"), limit=2, after=page.next_cursor
            )
            seen.extend(page.records)
        assert len(seen) == 5 and all(r.scope[:2] == ("origin", "account") for r in seen)
        for r in seen:
            assert tx.delete(r.table, r.key, expected_revision=r.revision)
        assert tx.get(Table.WEB_SESSIONS, "foreign") is not None
    store.close()


def test_real_process_crash_keeps_commit_and_drops_partial_write(tmp_path: Path) -> None:
    keys, config = MemoryKeys(), Config(tmp_path / "state")
    store = persistence.open_store(config, keys)
    store.close()
    # Forking a threaded/CoreFoundation process is unsafe on macOS. Start a
    # fresh interpreter and pass only these synthetic keys through stdin.
    script = """
import json
import os
import sys
from pathlib import Path
from relaydeck.companion import persistence
from relaydeck.persistence_contracts import Record, Table
from tests.support.persistence.persistence import Config, MemoryKeys, record

keys = MemoryKeys()
keys.keys = {name: bytes.fromhex(value) for name, value in json.load(sys.stdin).items()}
store = persistence.open_store(Config(Path(sys.argv[1])), keys)
with store.transaction() as tx:
    assert tx.put(
        Record(Table.REVOCATIONS, "committed", ("account",), b"revoked", 1, 1),
        expected_revision=None,
    )
with store.transaction() as tx:
    assert tx.put(record("partial"), expected_revision=None)
    os._exit(23)
"""
    child = subprocess.run(
        [sys.executable, "-B", "-c", script, str(config.state_dir)],
        input=json.dumps({name: value.hex() for name, value in keys.keys.items()}),
        capture_output=True,
        text=True,
        timeout=20,
        check=False,
    )
    assert child.returncode == 23, child.stderr
    restored = persistence.open_store(config, keys)
    try:
        with restored.transaction() as tx:
            committed = tx.get(Table.REVOCATIONS, "committed")
            assert committed is not None and committed.body == b"revoked"
            assert tx.get(Table.WEB_SESSIONS, "partial") is None
            assert tx.put(record("after-reopen"), expected_revision=None)
        with restored.transaction() as tx:
            assert tx.get(Table.WEB_SESSIONS, "after-reopen") == record("after-reopen")
    finally:
        restored.close()


def _r2_keychain_sdk(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> tuple[Any, Any]:
    import ctypes as ct

    from relaydeck.companion import credentials

    refs: dict[int, Any] = {}
    constants: dict[str, int] = {}

    def ref(value: Any) -> int:
        index = 100 + len(refs)
        refs[index] = value
        return index

    def constant(name: str) -> int:
        return constants.setdefault(name, 10000 + len(constants))

    def set_ptr(pointer: Any, value: int) -> None:
        ct.cast(pointer, ct.POINTER(ct.c_void_p)).contents.value = value

    def address(value: int | ct.c_void_p) -> int:
        result = value if isinstance(value, int) else value.value
        assert result is not None
        return result

    class CF:
        def CFRelease(self, value: Any) -> None:
            pass

        def CFStringCreateWithCString(self, allocator: Any, value: bytes, encoding: int) -> int:
            return ref(value.decode())

        def CFDataCreate(self, allocator: Any, value: bytes, length: int) -> int:
            return ref(value[:length])

        def CFArrayCreate(self, allocator: Any, values: Any, count: int, callbacks: Any) -> int:
            return ref([values[i] for i in range(count)])

        def CFArrayGetCount(self, value: int | ct.c_void_p) -> int:
            return len(refs[address(value)])

        def CFArrayGetValueAtIndex(self, value: int | ct.c_void_p, index: int) -> int:
            return cast(int, refs[address(value)][index])

        def CFDictionaryCreate(
            self, allocator: Any, keys: Any, values: Any, count: int, *callbacks: Any
        ) -> int:
            return ref({keys[i]: values[i] for i in range(count)})

        def CFGetTypeID(self, value: ct.c_void_p) -> int:
            return 1

        def CFDataGetTypeID(self) -> int:
            return 1

        def CFDataGetLength(self, value: int | ct.c_void_p) -> int:
            return len(refs[address(value)])

        def CFDataGetBytePtr(self, value: int | ct.c_void_p) -> bytes:
            return cast(bytes, refs[address(value)])

    class Security:
        def __init__(self) -> None:
            self.items: dict[str, bytes] = {}
            self.acls: list[Any] = []
            self.item_access: dict[str, int] = {}
            self.interrupt = False
            self.ui: list[bool] = []
            self.locked = False

        def SecKeychainGetUserInteractionAllowed(self, pointer: Any) -> int:
            ct.cast(pointer, ct.POINTER(ct.c_ubyte)).contents.value = 1
            return 0

        def SecKeychainSetUserInteractionAllowed(self, state: bool) -> int:
            self.ui.append(bool(state))
            return 0

        def SecKeychainCopyDefault(self, pointer: Any) -> int:
            set_ptr(pointer, 123)
            return 0

        def SecKeychainGetStatus(self, chain: Any, pointer: Any) -> int:
            ct.cast(pointer, ct.POINTER(ct.c_uint32)).contents.value = 0 if self.locked else 1
            return 0

        def name(self, query: int) -> str:
            return cast(str, refs[refs[query][constant("kSecAttrAccount")]])

        def SecItemCopyMatching(self, query: int, result: Any) -> int:
            name = self.name(query)
            if name not in self.items:
                return -25300
            set_ptr(
                result, ref(name if constant("kSecReturnRef") in refs[query] else self.items[name])
            )
            return 0

        def SecTrustedApplicationCreateFromPath(self, path: Any, pointer: Any) -> int:
            assert path is None
            set_ptr(pointer, ref("current-interpreter"))
            return 0

        def SecAccessCreate(self, descriptor: Any, apps: int, pointer: Any) -> int:
            trusted = [refs[a] for a in refs[apps]]
            self.acls.append(trusted)
            set_ptr(pointer, ref(apps))
            return 0

        def SecKeychainItemCopyAccess(self, item: int, pointer: Any) -> int:
            set_ptr(pointer, self.item_access[refs[item]])
            return 0

        def SecAccessCopyMatchingACLList(self, access: int, authorization: int) -> int:
            assert authorization == constant("kSecACLAuthorizationDecrypt")
            return ref([access])

        def SecACLCopyContents(self, acl: int, apps: Any, description: Any, prompt: Any) -> int:
            set_ptr(apps, refs[acl])
            set_ptr(description, ref("private storage"))
            return 0

        def SecTrustedApplicationCopyData(self, application: int, pointer: Any) -> int:
            set_ptr(pointer, ref(refs[application].encode()))
            return 0

        def SecItemAdd(self, query: int, result: Any) -> int:
            values = refs[query]
            assert constant("kSecAttrAccess") in values
            name = self.name(query)
            if name in self.items:
                return -25299
            self.items[name] = refs[values[constant("kSecValueData")]]
            self.item_access[name] = values[constant("kSecAttrAccess")]
            if self.interrupt:
                raise OSError("SECRET_CANARY interrupted add")
            return 0

        def SecItemUpdate(self, query: int, update: int) -> int:
            name = self.name(query)
            if name not in self.items:
                return -25300
            self.items[name] = refs[refs[update][constant("kSecValueData")]]
            if self.interrupt:
                raise OSError("SECRET_CANARY interrupted update")
            return 0

        def SecItemDelete(self, query: int) -> int:
            self.items.pop(self.name(query), None)
            return 0

    backend = credentials._Keychain.__new__(credentials._Keychain)
    backend.service = "dev.relaydeck.persistence.test." + secrets.token_hex(8)
    monkeypatch.setattr(backend, "cf", CF(), raising=False)
    monkeypatch.setattr(backend, "sec", Security(), raising=False)
    monkeypatch.setattr(backend, "_constant", constant)
    monkeypatch.setattr(backend, "_lock_root", tmp_path, raising=False)
    backend.key_callbacks = credentials._KeyCallbacks()
    backend.value_callbacks = credentials._Callbacks()
    backend.array_callbacks = credentials._Callbacks()
    provider = credentials.MacKeyProvider.__new__(credentials.MacKeyProvider)
    provider._backend = backend
    return provider, backend.sec


def test_created_items_pin_current_interpreter_legacy_acl(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    provider, sdk = _r2_keychain_sdk(monkeypatch, tmp_path)
    assert len(provider.get("probe", create=True, allow_prompt=True)) == 32
    assert sdk.acls == [["current-interpreter"]]
    assert len(provider.get("probe")) == 32 and sdk.ui == [False, True]


def test_exact_secret_import_and_interrupted_durable_generation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    provider, sdk = _r2_keychain_sdk(monkeypatch, tmp_path)
    material = limits.SecretMaterial(b"\x00COOKIE_CANARY exact bytes\xff" * 3)
    sdk.interrupt = True
    with pytest.raises(KeyUnavailable):
        provider.import_secret("cookie", material, allow_prompt=True)
    sdk.interrupt = False
    assert provider.import_secret("cookie", material) == material
    assert provider.read_secret("cookie") == material
    with pytest.raises(KeyUnavailable):
        provider.import_secret("cookie", limits.SecretMaterial(b"replacement"))
    before = provider.restore_generation("auth", create=True, allow_prompt=True)
    sdk.interrupt = True
    with pytest.raises(KeyUnavailable):
        provider.advance_restore_generation("auth")
    sdk.interrupt = False
    after = provider.restore_generation("auth")
    assert after.value > before.value
    assert provider.advance_restore_generation("auth").value > after.value
    assert provider.read_secret("cookie") == material
    assert "COOKIE_CANARY" not in caplog.text and "SECRET_CANARY" not in caplog.text


def test_generation_advances_are_serialized(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    provider, sdk = _r2_keychain_sdk(monkeypatch, tmp_path)
    provider.restore_generation("auth", create=True, allow_prompt=True)
    results: list[int] = []
    threads = [
        threading.Thread(
            target=lambda: results.append(provider.advance_restore_generation("auth").value)
        )
        for _ in range(8)
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(2)
    assert sorted(results) == list(range(1, 9))
    assert provider.restore_generation("auth").value == 8


def test_native_acceptance_probes_validate_acl_and_lock_without_ui(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    provider, sdk = _r2_keychain_sdk(monkeypatch, tmp_path)
    provider.get("probe", create=True, allow_prompt=True)
    assert provider._backend.trusts_current_application("probe")
    assert not provider._backend.is_locked()
    sdk.locked = True
    assert provider._backend.is_locked()
    with pytest.raises(KeyUnavailable):
        provider.get("probe")
    assert sdk.ui == [False, True, False, True]


def test_explicit_setup_temporarily_enables_previously_disabled_ui(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import ctypes as ct

    provider, sdk = _r2_keychain_sdk(monkeypatch, tmp_path)

    def disabled(pointer: Any) -> int:
        ct.cast(pointer, ct.POINTER(ct.c_ubyte)).contents.value = 0
        return 0

    monkeypatch.setattr(sdk, "SecKeychainGetUserInteractionAllowed", disabled)
    provider.get("probe", create=True, allow_prompt=True)
    assert sdk.ui == [True, False]


@pytest.mark.parametrize("raw", [b"", b"x", bytes(range(256)) * 8])
def test_import_is_arbitrary_bytes_not_aead_material(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    raw: bytes,
) -> None:
    provider, sdk = _r2_keychain_sdk(monkeypatch, tmp_path)
    material = limits.SecretMaterial(raw)
    assert provider.import_secret("signing", material, allow_prompt=True) == material
    assert provider.import_secret("signing", material) == material
    assert provider.read_secret("signing") == material
    with pytest.raises(KeyUnavailable):
        provider.get("material/signing")
    with pytest.raises(KeyUnavailable):
        provider.restore_generation("missing")
    with pytest.raises(KeyUnavailable):
        provider.advance_restore_generation("missing")
    assert "restore/missing" not in sdk.items


def test_draft_sdk_calls_use_one_worker_and_notice_does_no_io(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    keys = MemoryKeys()
    store = persistence.open_store(Config(tmp_path / "state"), keys)
    drafts = store.scoped_drafts("origin", "account", "device", "tab")
    main_thread = threading.get_ident()
    threads: list[int] = []
    original = keys.get

    def get(name: str, *, create: bool = False, allow_prompt: bool = False) -> bytes:
        threads.append(threading.get_ident())
        return original(name, create=create, allow_prompt=allow_prompt)

    monkeypatch.setattr(keys, "get", get)

    async def exercise() -> None:
        target = DraftTarget("host", "terminal")
        saved = await drafts.save(target, "draft", None)
        assert await drafts.load(target) == saved
        assert await drafts.delete_revision(target, saved.revision)
        assert await drafts.available()
        await drafts.clear_account()
        count = len(threads)
        drafts.notice()
        assert len(threads) == count

    try:
        asyncio.run(exercise())
        assert threads and len(set(threads)) == 1 and main_thread not in threads
    finally:
        store.close()


def test_continuous_unique_target_churn_cannot_outgrow_pruning(tmp_path: Path) -> None:
    store = persistence.open_store(Config(tmp_path / "state"), MemoryKeys())
    drafts = store.scoped_drafts("origin", "account", "device", "tab")

    async def churn() -> None:
        for index in range(1000):
            target = DraftTarget("host", str(index))
            saved = await drafts.save(target, "churn", None)
            assert await drafts.delete_revision(target, saved.revision)

    try:
        asyncio.run(churn())
        counters = store._db.execute(
            "SELECT count(*) FROM records WHERE table_name='draft_revisions'"
        ).fetchone()[0]
        assert counters <= limits.MAX_DRAFT_TOMBSTONES + limits.MAX_SWEEP_ROWS
        assert store._db.execute("SELECT count(*) FROM nonces").fetchone()[0] <= 2 * (
            limits.MAX_NONCE_HISTORY + counters
        )
    finally:
        store.close()


@pytest.mark.parametrize("touches", [0, 2])
def test_same_target_cas_and_read_touch_never_stall_without_sweep(
    tmp_path: Path,
    touches: int,
) -> None:
    keys, config = MemoryKeys(), Config(tmp_path / "state")
    store = persistence.open_store(config, keys)
    drafts = store.scoped_drafts("origin", "account", "device", "tab")
    target = DraftTarget("host", "terminal")

    async def edit() -> limits.DraftRecord:
        latest = await drafts.save(target, "start", None)
        for index in range(1105):
            latest = await drafts.save(target, str(index), latest.revision)
            for _ in range(touches):
                assert await drafts.load(target) == latest
        with pytest.raises(StorageUnavailable, match="revision_conflict"):
            await drafts.save(target, "stale", latest.revision - 1)
        assert await drafts.load(target) == latest
        return latest

    try:
        latest = asyncio.run(edit())
        assert store._db.execute("SELECT count(*) FROM nonces").fetchone()[0] <= 2 * (
            limits.MAX_NONCE_HISTORY + 2
        )
    finally:
        store.close()
    reopened = persistence.open_store(config, keys)
    try:
        restored = reopened.scoped_drafts("origin", "account", "device", "tab")
        assert asyncio.run(restored.load(target)) == latest
        updated = asyncio.run(restored.save(target, "after restart", latest.revision))
        assert updated.revision > latest.revision
    finally:
        reopened.close()
