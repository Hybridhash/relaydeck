"""File-level bounds, restart semantics and atomic owner/device races."""

from __future__ import annotations

import json
import os
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from relaydeck.contracts.pairing import MAX_REQUESTS
from relaydeck.gateway.pairing import PairingError, PairingStore
from tests.support.pairing import ORIGIN, VERIFIER, foundation, proof


def test_window_required_and_exact_expiry_restart(tmp_path: Path) -> None:
    state, now, body = foundation(tmp_path / "state")
    store = state.pairing
    assert store is not None
    host = state.hosts.record_for_host("home-mac")
    with pytest.raises(PairingError):
        store.request(body, host, now[0])
    for ttl in (0, 301, True):
        with pytest.raises(ValueError):
            store.open_window(host, ORIGIN, now[0], ttl)
    store.open_window(host, ORIGIN, now[0], 1)
    row = store.request(body, host, now[0])
    store.decide(row["request_id"], row["matching_code"], host, ORIGIN, now[0], approve=True)
    with pytest.raises(PairingError):
        store.redeem(row["request_id"], proof(), host, now[0] + 1000)
    store.open_window(host, ORIGIN, now[0], 120)
    row = store.request(body, host, now[0])
    store.decide(row["request_id"], row["matching_code"], host, ORIGIN, now[0], approve=True)
    restarted = PairingStore(store.path)
    restarted.start(ORIGIN)
    for reader in (store, restarted):
        with pytest.raises(PairingError):
            reader.redeem(row["request_id"], proof(), host, now[0])
    assert json.loads(store.path.read_text())["window"] is None


def test_creation_bounds_never_evict_into_an_open_queue(tmp_path: Path) -> None:
    state, now, body = foundation(tmp_path / "state")
    store = state.pairing
    assert store is not None
    host = state.hosts.record_for_host("home-mac")
    store.open_window(host, ORIGIN, now[0], 300)
    for count in range(MAX_REQUESTS):
        if count and count % 5 == 0:
            now[0] += 10_000
        store.request(body, host, now[0])
        if count == 4:
            with pytest.raises(PairingError, match="rate"):
                store.request(body, host, now[0])
    now[0] += 10_000
    with pytest.raises(PairingError, match="limit"):
        store.request(body, host, now[0])
    assert len(json.loads(store.path.read_text())["requests"]) == MAX_REQUESTS
    store.close_window()
    with pytest.raises(PairingError):
        store.request(body, host, now[0])


def test_polling_floor_and_budget(tmp_path: Path) -> None:
    state, now, body = foundation(tmp_path / "state")
    store = state.pairing
    assert store is not None
    host = state.hosts.record_for_host("home-mac")
    store.open_window(host, ORIGIN, now[0], 300)
    row = store.request(body, host, now[0])
    assert store.poll(row["request_id"], proof(), host, now[0])["state"] == "pending"
    with pytest.raises(PairingError, match="polling"):
        store.poll(row["request_id"], proof(), host, now[0] + 1999)
    for i in range(1, 150):
        store.poll(row["request_id"], proof(), host, now[0] + i * 2000)
    # At the exact request deadline it is expired, even though the window is open.
    with pytest.raises(PairingError):
        store.poll(row["request_id"], proof(), host, now[0] + 300_000)


def test_concurrent_redeem_across_store_instances_consumes_once(tmp_path: Path) -> None:
    state, now, body = foundation(tmp_path / "state")
    store = state.pairing
    assert store is not None
    host = state.hosts.record_for_host("home-mac")
    store.open_window(host, ORIGIN, now[0], 120)
    row = store.request(body, host, now[0])
    store.decide(row["request_id"], row["matching_code"], host, ORIGIN, now[0], approve=True)

    def redeem(_: int) -> str:
        try:
            return (
                PairingStore(store.path).redeem(row["request_id"], proof(), host, now[0]).device_id
            )
        except PairingError:
            return "rejected"

    with ThreadPoolExecutor(max_workers=8) as executor:
        results = list(executor.map(redeem, range(8)))
    assert results.count(row["device_id"]) == 1
    assert results.count("rejected") == 7
    assert VERIFIER not in store.path.read_text()


@pytest.mark.parametrize("target", ["state", "file", "file-link", "lock", "ancestor", "hardlink"])
def test_unsafe_local_paths_rejected(tmp_path: Path, target: str) -> None:
    state, _, _ = foundation(tmp_path / "state")
    assert state.pairing is not None
    path = state.pairing.path
    if target == "state":
        path.parent.chmod(0o755)
    elif target == "file":
        path.chmod(0o644)
    elif target == "file-link":
        real = path.with_suffix(".real")
        path.rename(real)
        path.symlink_to(real)
    elif target == "lock":
        path.with_suffix(".lock").unlink()
        path.with_suffix(".lock").symlink_to(path)
    elif target == "ancestor":
        link = tmp_path / "alias"
        link.symlink_to(path.parent, target_is_directory=True)
        path = link / path.name
    else:
        os.link(path, path.with_suffix(".extra"))
    with pytest.raises(ValueError):
        PairingStore(path)


def test_other_uid_and_corrupt_state_fail_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    state, now, body = foundation(tmp_path / "state")
    store = state.pairing
    assert store is not None
    host = state.hosts.record_for_host("home-mac")
    actual_uid = os.getuid()
    with monkeypatch.context() as patch:
        patch.setattr(os, "getuid", lambda: actual_uid + 1)
        with pytest.raises(ValueError, match="owner-only"):
            store.open_window(host, ORIGIN, now[0], 120)
    store.path.write_text("{corrupt")
    with pytest.raises(PairingError):
        store.request(body, host, now[0])
    assert store.path.read_text() == "{corrupt"


def test_missing_enrollment_and_wrong_matching_code(tmp_path: Path) -> None:
    state, now, body = foundation(tmp_path / "state")
    store = state.pairing
    assert store is not None
    with pytest.raises(PairingError, match="Enroll this Mac"):
        store.open_window(None, ORIGIN, now[0], 120)
    host = state.hosts.record_for_host("home-mac")
    store.open_window(host, ORIGIN, now[0], 120)
    row = store.request(body, host, now[0])
    with pytest.raises(PairingError):
        store.decide(row["request_id"], "not-the-code", host, ORIGIN, now[0], approve=True)
    store.decide(row["request_id"], row["matching_code"], host, ORIGIN, now[0], approve=False)
    assert store.poll(row["request_id"], proof(), host, now[0])["state"] == "denied"
    with pytest.raises(PairingError):
        store.redeem(row["request_id"], proof(), host, now[0])


def test_denial_and_redemption_race_and_backward_clock_fail_closed(tmp_path: Path) -> None:
    state, now, body = foundation(tmp_path / "state")
    store = state.pairing
    assert store is not None
    host = state.hosts.record_for_host("home-mac")
    store.open_window(host, ORIGIN, now[0], 120)
    row = store.request(body, host, now[0])
    store.decide(row["request_id"], row["matching_code"], host, ORIGIN, now[0], approve=True)
    with pytest.raises(PairingError):
        store.redeem(row["request_id"], proof(), host, now[0] - 1)

    def run(action: str) -> str:
        reader = PairingStore(store.path)
        try:
            if action == "deny":
                reader.decide(
                    row["request_id"], row["matching_code"], host, ORIGIN, now[0], approve=False
                )
            else:
                reader.redeem(row["request_id"], proof(), host, now[0])
            return action
        except PairingError:
            return "rejected"

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(run, ("deny", "redeem")))
    assert results.count("rejected") == 1
    terminal_state = json.loads(store.path.read_text())["requests"][row["request_id"]]["state"]
    assert terminal_state == ("denied" if "deny" in results else "consumed")


def test_global_poll_rate_and_enrollment_validation(tmp_path: Path) -> None:
    from dataclasses import replace

    state, now, body = foundation(tmp_path / "state")
    store = state.pairing
    assert store is not None
    host = state.hosts.record_for_host("home-mac")
    assert host is not None
    with pytest.raises(PairingError):
        store.open_window(replace(host, fingerprint="invalid"), ORIGIN, now[0], 120)
    store.open_window(host, ORIGIN, now[0], 120)
    row = store.request(body, host, now[0])
    store.poll(row["request_id"], proof(), host, now[0])
    for _ in range(58):
        with pytest.raises(PairingError, match="polling"):
            store.poll(row["request_id"], proof(), host, now[0])
    with pytest.raises(PairingError, match="rate exceeded"):
        store.poll(row["request_id"], proof(), host, now[0])
