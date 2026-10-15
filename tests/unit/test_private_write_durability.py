"""Private state writers survive short writes and leave no temp file behind on failure."""

import os
import stat
from pathlib import Path

import pytest

from relaydeck.connector import enrollment
from relaydeck.connector.enrollment import ConnectorCreds, load_creds, save_creds
from relaydeck.usage.limits_plane import refresh_state
from relaydeck.usage.limits_plane.refresh_state import RefreshState


def _one_byte_writes(monkeypatch: pytest.MonkeyPatch, module: object) -> None:
    real_write = os.write

    def short_write(fd: int, data: bytes) -> int:
        return real_write(fd, bytes(data[:1]))

    monkeypatch.setattr(module.os, "write", short_write)  # type: ignore[attr-defined]


def test_credentials_survive_short_writes(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _one_byte_writes(monkeypatch, enrollment)
    target = tmp_path / "connector.json"
    creds = ConnectorCreds("https://gateway.example", "host-1", "token-" + "x" * 64)
    save_creds(creds, str(target))
    assert load_creds(str(target)) == creds
    assert stat.S_IMODE(target.stat().st_mode) == 0o600


def test_failed_credentials_replace_keeps_the_old_file_and_no_temp(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = tmp_path / "connector.json"
    old = ConnectorCreds("https://gateway.example", "host-1", "old-token")
    save_creds(old, str(target))

    def failing_replace(src: object, dst: object) -> None:
        raise OSError("disk full")

    monkeypatch.setattr(enrollment.os, "replace", failing_replace)
    with pytest.raises(OSError):
        save_creds(ConnectorCreds("https://gateway.example", "host-1", "new"), str(target))
    assert load_creds(str(target)) == old
    assert not target.with_suffix(".tmp").exists()


def test_refresh_state_survives_short_writes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _one_byte_writes(monkeypatch, refresh_state)
    path = tmp_path / "limits-refresh.json"
    RefreshState(path, clock_ms=lambda: 1_000).set_cooldown("alpha", 5_000)
    assert RefreshState(path, clock_ms=lambda: 1_000).cooldown_until("alpha") == 5_000


def test_refresh_state_never_raises_when_its_directory_cannot_be_made(tmp_path: Path) -> None:
    blocker = tmp_path / "not-a-dir"
    blocker.write_text("")
    state = RefreshState(blocker / "limits-refresh.json", clock_ms=lambda: 1_000)
    state.set_cooldown("alpha", 5_000)  # the save fails quietly; memory keeps the value
    assert state.cooldown_until("alpha") == 5_000
