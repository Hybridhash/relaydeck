"""State validation shares the atomic writer's cross-thread/process file lock."""

from __future__ import annotations

import fcntl
import os
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

import pytest

import relaydeck.gateway.pairing as pairing
from relaydeck.companion.private import write_json
from tests.support.pairing import ORIGIN, foundation


@pytest.mark.parametrize("new_store", [False, True], ids=["transaction", "constructor"])
def test_state_link_check_waits_for_atomic_replace(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, new_store: bool
) -> None:
    state, now, _ = foundation(tmp_path / "state")
    store = state.pairing
    assert store is not None
    store.open_window(state.hosts.record_for_host("home-mac"), ORIGIN, now[0], 120)
    writing, reading, replace_allowed, writer_done = (threading.Event() for _ in range(4))
    local = threading.local()
    observed_links: list[int] = []
    real_write, real_lstat, real_flock = write_json, Path.lstat, fcntl.flock

    def held_write(path: Path, data: dict[str, Any]) -> None:
        if not getattr(local, "reader", False):
            writing.set()
            assert replace_allowed.wait(10), "reader did not reach validation or flock"
        real_write(path, data)

    def lstat(path: Path) -> os.stat_result:
        if getattr(local, "reader", False) and path == store.path:
            local.state_stats += 1
            if local.state_stats == 2 and not writer_done.is_set():
                # Model lookup preceding rename and metadata read following it.
                # The old inode's zero link count comes from the real replacement,
                # not fabricated stat fields. A locked reader cannot enter here.
                fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
                try:
                    reading.set()
                    assert writer_done.wait(10), "atomic writer did not settle"
                    info = os.fstat(fd)
                    observed_links.append(info.st_nlink)
                    return info
                finally:
                    os.close(fd)
        return real_lstat(path)

    def flock(fd: int, operation: int) -> None:
        if getattr(local, "reader", False):
            reading.set()
        real_flock(fd, operation)

    def writer() -> None:
        try:
            store.close_window()
        finally:
            writer_done.set()

    def reader() -> object:
        local.reader, local.state_stats = True, 0
        current = pairing.PairingStore(store.path) if new_store else store
        with current._transaction() as data:
            return data["window"]

    monkeypatch.setattr(pairing, "write_json", held_write)
    monkeypatch.setattr(Path, "lstat", lstat)
    monkeypatch.setattr(fcntl, "flock", flock)
    with ThreadPoolExecutor(max_workers=2) as executor:
        write = executor.submit(writer)
        try:
            assert writing.wait(10), "writer did not acquire the pairing lock"
            read = executor.submit(reader)
            assert reading.wait(10), "reader did not start"
        finally:
            replace_allowed.set()
        write.result(timeout=10)
        assert read.result(timeout=10) is None
    assert observed_links == [], "state was inspected before acquiring its writer's lock"


@pytest.mark.parametrize("target", ["state", "lock"])
@pytest.mark.parametrize("existing_store", [False, True])
def test_hardlinks_rejected_before_read_or_replace(
    tmp_path: Path, target: str, existing_store: bool
) -> None:
    state, _, _ = foundation(tmp_path / "state")
    store = state.pairing
    assert store is not None
    path = store.path if target == "state" else store.path.with_suffix(".lock")
    original = store.path.read_bytes()
    os.link(path, path.with_suffix(".extra"))
    with pytest.raises(ValueError, match="multiple links|owner-private"):
        if existing_store:
            store.close_window()
        else:
            pairing.PairingStore(store.path)
    assert store.path.read_bytes() == original
    assert path.stat().st_nlink == 2
