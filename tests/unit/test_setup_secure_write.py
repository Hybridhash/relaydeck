"""Secure setup writes: consented creation through safe directories, and a
locked compare-and-swap for edits that never loses a concurrent change."""

from __future__ import annotations

import fcntl
import os
import stat
from pathlib import Path

import pytest

from relaydeck.companion import config_edit, setup
from relaydeck.companion.detect import DetectReport
from tests.support.companion.asgi_companion import config_file
from tests.support.setup.prereqs import Serve, probes

TARGET = "http://127.0.0.1:8125"
HOSTPORT = "mac.synthetic.ts.net:8445"


def _mapping(target: str = TARGET) -> dict[str, object]:
    return {
        "TCP": {"8445": {"HTTPS": True}},
        "Web": {HOSTPORT: {"Handlers": {"/": {"Proxy": target}}}},
    }


def _private_config(directory: Path, text: str = "port = 8125\n") -> Path:
    path = directory / "companion.toml"
    path.write_text(text, encoding="utf-8")
    path.chmod(0o600)
    return path


# -- creation: safe directories only -----------------------------------------


def test_creation_through_a_symlinked_parent_is_refused(tmp_path: Path) -> None:
    root = tmp_path.resolve()
    real = root / "real"
    real.mkdir(mode=0o755)
    (root / "link").symlink_to(real, target_is_directory=True)
    message = setup.ensure_config(root / "link" / "companion.toml")
    assert "not created" in message
    assert oct(os.stat(real).st_mode & 0o777) == "0o755"
    assert not (real / "companion.toml").exists()


def test_creation_under_a_group_writable_directory_is_refused(tmp_path: Path) -> None:
    root = tmp_path.resolve()
    loose = root / "loose"
    loose.mkdir(mode=0o775)
    message = setup.ensure_config(loose / "companion.toml")
    assert "not created" in message
    assert oct(os.stat(loose).st_mode & 0o777) == "0o775"
    assert not (loose / "companion.toml").exists()


def test_creation_still_makes_an_owner_only_config(tmp_path: Path) -> None:
    root = tmp_path.resolve()
    path = root / "cfg" / "companion.toml"
    assert "created" in setup.ensure_config(path)
    assert oct(os.stat(path).st_mode & 0o777) == "0o600"
    assert oct(os.stat(path.parent).st_mode & 0o777) == "0o700"


# -- creation: separate consent ----------------------------------------------


def _quiet_setup(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(setup, "detect_report", lambda _path: DetectReport((), "ok", "ok"))


def test_missing_config_is_not_created_without_consent_even_with_yes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path.resolve()
    (root / "socket").touch()
    missing = root / "companion.toml"
    serve = Serve(serve=_mapping())
    _quiet_setup(monkeypatch)
    asked: list[str] = []

    def confirm(prompt: str) -> bool:
        asked.append(prompt)
        return False

    report = setup.run_setup(
        config_path=missing,
        assume_yes=True,
        confirm=confirm,
        probes=probes(serve),
        runner=serve,
    )
    assert report.exit_code == 1
    assert not missing.exists()
    assert any("not authorized" in line for line in report.lines)
    assert any("Create the owner-only config" in question for question in asked)


def test_missing_config_is_created_once_consented(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path.resolve()
    (root / "socket").touch()
    missing = root / "companion.toml"
    serve = Serve(serve=_mapping())
    _quiet_setup(monkeypatch)
    report = setup.run_setup(
        config_path=missing,
        assume_yes=False,
        confirm=lambda _: True,
        probes=probes(serve),
        runner=serve,
    )
    assert missing.is_file()
    assert any("created" in line for line in report.lines)


def test_existing_config_never_prompts_for_creation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path.resolve()
    socket = root / "socket"
    socket.touch()
    profile = config_file(root, herdr_socket=str(socket), port=8129)
    serve = Serve(serve=_mapping("http://127.0.0.1:8129"))
    _quiet_setup(monkeypatch)
    asked: list[str] = []

    def confirm(prompt: str) -> bool:
        asked.append(prompt)
        return False

    setup.run_setup(
        config_path=profile,
        assume_yes=True,
        confirm=confirm,
        probes=probes(serve),
        runner=serve,
    )
    assert asked == []


# -- compare-and-swap ---------------------------------------------------------


def test_replace_aborts_when_the_live_file_differs(tmp_path: Path) -> None:
    root = tmp_path.resolve()
    path = _private_config(root, "port = 8125\n")
    with pytest.raises(config_edit.ConcurrentEdit):
        config_edit.replace_if_match(
            path, "port = 9999\n", "port = 1\n", backup_name="companion.toml.bak-t"
        )
    assert path.read_text(encoding="utf-8") == "port = 8125\n"
    assert not (root / "companion.toml.bak-t").exists()


def test_replace_aborts_when_another_writer_holds_the_lock(tmp_path: Path) -> None:
    root = tmp_path.resolve()
    path = _private_config(root)
    lock_fd = os.open(root / "companion.toml.lock", os.O_RDWR | os.O_CREAT, 0o600)
    try:
        fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        with pytest.raises(config_edit.ConfigLocked):
            config_edit.replace_if_match(
                path,
                "port = 8125\n",
                "port = 1\n",
                backup_name="companion.toml.bak-t",
            )
    finally:
        os.close(lock_fd)
    assert path.read_text(encoding="utf-8") == "port = 8125\n"


def test_apply_reports_a_post_commit_receipt_failure_truthfully(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path.resolve()
    path = config_file(root)
    before = path.read_text(encoding="utf-8")
    after = before.replace('"missing-herdr.sock"', '"other.sock"')

    def broken_receipt(*args: object, **kwargs: object) -> Path:
        raise OSError("receipt disk full")

    monkeypatch.setattr(setup, "_receipt", broken_receipt)
    lines = setup.apply_text(path, before, after, action="add", entries=[])
    assert path.read_text(encoding="utf-8") == after
    assert any("were written" in line for line in lines)
    assert any("Backup kept" in line for line in lines)
    assert not any("no source was written" in line for line in lines)


def test_an_edit_saved_during_staging_aborts_and_survives(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path.resolve()
    path = _private_config(root)
    before = path.read_text(encoding="utf-8")
    late = before.replace("8125", "9999")
    real_fsync = os.fsync
    injected = False

    def fsync_and_inject(fd: int) -> None:
        nonlocal injected
        if not injected:
            injected = True
            # An ordinary editor save landing after the first check, while the
            # backup and replacement are still being staged.
            path.write_text(late, encoding="utf-8")
        real_fsync(fd)

    monkeypatch.setattr(os, "fsync", fsync_and_inject)
    with pytest.raises(config_edit.ConcurrentEdit):
        config_edit.replace_if_match(
            path, before, before.replace("8125", "8126"), backup_name="companion.toml.bak-late"
        )
    assert injected
    assert path.read_text(encoding="utf-8") == late


def test_a_post_rename_sync_failure_reports_the_change_truthfully(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path.resolve()
    path = config_file(root)
    before = path.read_text(encoding="utf-8")
    after = before.replace('"missing-herdr.sock"', '"other.sock"')
    real_fsync = os.fsync

    def fail_dir_fsync(fd: int) -> None:
        if stat.S_ISDIR(os.fstat(fd).st_mode):
            raise OSError("parent sync failed")
        real_fsync(fd)

    monkeypatch.setattr(os, "fsync", fail_dir_fsync)
    lines = setup.apply_text(path, before, after, action="add", entries=[])
    text = "\n".join(lines)
    assert "was changed" in text
    assert "companion.toml.bak-" in text
    assert "before retrying" in text
    assert "nothing was restored automatically" in text
    assert "unchanged" not in text
    assert path.read_text(encoding="utf-8") == after
    (backup,) = sorted(path.parent.glob("companion.toml.bak-*"))
    assert backup.read_text(encoding="utf-8") == before
