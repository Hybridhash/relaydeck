"""The Notifications item counts queued and retrying web push messages.

The check is read-only: it opens the store without creating or changing it. Temporary home only.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec

from relaydeck.companion import readiness
from relaydeck.companion.config import load_config
from relaydeck.gateway import webpush
from relaydeck.gateway.webpush_store import WebPushStore, outbox_counts
from tests.support.companion.setup_sources import setup_home

NOW = 1_700_000_000_000
ENDPOINT = "https://fcm.googleapis.com/fcm/send/synthetic-endpoint"


@pytest.fixture
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    fake = tmp_path / "home"
    fake.mkdir(mode=0o700)
    monkeypatch.setenv("HOME", str(fake))
    return fake


def _config(home: Path, extra: str = "") -> Any:
    path, _report = setup_home(home)
    if extra:
        path.write_text(path.read_text(encoding="utf-8") + extra, encoding="utf-8")
    return load_config(path)


def _seed(state: Path, queued: int, retrying: int) -> None:
    store = WebPushStore(state, clock=lambda: NOW)
    pub = (
        ec.generate_private_key(ec.SECP256R1())
        .public_key()
        .public_bytes(serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint)
    )
    store.put_subscription(
        "acct", "dev", ENDPOINT, webpush.b64u(pub), webpush.b64u(b"\x01" * 16), NOW
    )
    for index in range(queued):
        store.enqueue("acct", "dev", f"event-{index}", "attention", now=NOW)
    for row in store.due(NOW, limit=retrying):
        store.retry_or_drop(row, NOW)
    store.close()


def _item(config: object) -> readiness.Item:
    return readiness._notifications_item(config)  # type: ignore[arg-type]


def test_no_store_yet_is_ok_and_counts_nothing(home: Path) -> None:
    config = _config(home)
    assert outbox_counts(config.state_dir) is None
    item = _item(config)
    assert item.state == readiness.OK and item.number == 10
    assert item.reason == "no web push messages waiting"


def test_queued_and_retrying_messages_are_counted(home: Path) -> None:
    config = _config(home)
    _seed(config.state_dir, queued=3, retrying=1)
    assert outbox_counts(config.state_dir) == (3, 1)
    item = _item(config)
    assert item.state == readiness.WARN
    assert "3 web push message(s) waiting, 1 retrying" in item.reason
    assert item.next_step.startswith("check the companion log")


def test_the_count_is_informational_and_never_blocks_pairing(home: Path) -> None:
    path, _report = setup_home(home)
    _seed(load_config(path).state_dir, queued=2, retrying=0)
    report = readiness.run_check(
        path,
        prereq_report=None,
        companion_state=lambda _state: "running",
        web_current=lambda: None,
        usage=None,
    )
    notifications = next(item for item in report.items if item.number == 10)
    assert notifications.state == readiness.WARN
    pairing = next(item for item in report.items if item.number == 11)
    assert not re.search(r"\b10\b", pairing.reason)  # the waiting count is not a pairing blocker


def test_the_check_never_creates_or_changes_the_store(home: Path) -> None:
    config = _config(home)
    _seed(config.state_dir, queued=2, retrying=1)
    directory = config.state_dir / "webpush"
    before = {p.name: p.read_bytes() for p in directory.iterdir() if p.is_file()}
    _item(config)
    outbox_counts(config.state_dir)
    after = {p.name: p.read_bytes() for p in directory.iterdir() if p.is_file()}
    assert set(after) == set(before)  # no new file
    assert after["webpush.db"] == before["webpush.db"]


def test_web_push_off_is_reported_as_off_not_counted(home: Path) -> None:
    config = _config(home, "\n[notifications]\nweb_push = false\n")
    item = _item(config)
    assert item.state == readiness.OK
    assert "web push is off" in item.reason


def test_a_missing_configuration_is_not_checked(home: Path) -> None:
    item = _item(None)
    assert item.state == readiness.OK and item.number == 10
    assert "not checked" in item.reason


def test_the_pairing_item_is_now_number_eleven() -> None:
    assert readiness.TITLES[10] == "Notifications"
    assert readiness.TITLES[11] == "Pairing"
    assert 10 not in readiness.PAIRING_ITEMS and 11 not in readiness.PAIRING_ITEMS
