"""Opt-in transactions use synthetic owner files, never the owner's CLI settings."""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

from relaydeck.usage import hook_files as fs
from relaydeck.usage.hook_install import HookResult, install_hook, uninstall_hook
from relaydeck.usage.hook_setup import HOOK_FILENAME, HOOK_SHA256, managed_command


def write(path: Path, value: dict[str, Any]) -> None:
    path.write_text(json.dumps(value))
    path.chmod(0o600)


@dataclass
class Files:
    settings: Path
    cache: Path
    hooks: Path
    python: Path

    @property
    def script(self) -> Path:
        return self.hooks / HOOK_FILENAME

    @property
    def receipt(self) -> Path:
        return self.hooks / (
            "limits-" + hashlib.sha256(os.fsencode(self.settings)).hexdigest() + ".json"
        )

    def install(self, **kwargs: Any) -> HookResult:
        return install_hook(
            self.settings, self.cache, hooks_dir=self.hooks, interpreter=self.python, **kwargs
        )

    def uninstall(self, **kwargs: Any) -> HookResult:
        return uninstall_hook(
            self.settings, self.cache, hooks_dir=self.hooks, interpreter=self.python, **kwargs
        )


@pytest.fixture
def files(tmp_path: Path) -> Files:
    root = tmp_path.resolve()
    settings = root / "source settings.json"
    write(settings, {"unrelated": {"name": "private-marker", "value": [True, None, 7]}})
    # Preserve a virtualenv/tool-environment symlink rather than its base Python.
    python = root / "tool env/python"
    python.parent.mkdir(mode=0o700)
    python.symlink_to(Path(sys.executable))
    return Files(settings, root / "private state/cache.json", root / "hooks", python)


def test_opt_in_and_separate_missing_file_creation(files: Files) -> None:
    before = files.settings.read_bytes()
    assert files.install().state == "confirmation_required"
    assert files.uninstall().state == "confirmation_required"
    assert files.settings.read_bytes() == before and not files.hooks.exists()
    files.settings.unlink()
    assert files.install(confirmed=True).state == "create_confirmation_required"
    assert not files.hooks.exists() and not files.settings.exists()
    assert files.install(confirmed=True, create_settings=True).state == "installed"
    assert files.uninstall(confirmed=True).state == "uninstalled"
    assert json.loads(files.settings.read_text()) == {}  # Do not delete later settings.


def test_missing_settings_directory_needs_creation_consent(files: Files) -> None:
    files.settings = files.settings.parent / "new source/settings.json"
    assert files.install(confirmed=True).state == "create_confirmation_required"
    assert not files.settings.parent.exists() and not files.hooks.exists()
    assert files.install(confirmed=True, create_settings=True).state == "installed"
    assert files.settings.parent.stat().st_mode & 0o777 == 0o700


def test_private_install_and_idempotence_with_exact_quoted_interpreter(files: Files) -> None:
    before = files.settings.read_bytes()
    assert files.install(confirmed=True) == HookResult("installed", True, True)
    installed = json.loads(files.settings.read_text())
    assert installed == {
        **json.loads(before),
        "statusLine": {
            "type": "command",
            "command": managed_command(files.script, files.cache, files.python),
        },
    }
    assert str(files.python) in installed["statusLine"]["command"]
    assert hashlib.sha256(files.script.read_bytes()).hexdigest() == HOOK_SHA256
    assert files.hooks.stat().st_mode & 0o777 == 0o700
    assert files.cache.parent.stat().st_mode & 0o777 == 0o700
    assert not files.cache.exists()  # Installation never fabricates a quota observation.
    for path in (files.settings, *files.hooks.iterdir()):
        assert path.stat().st_mode & 0o777 == 0o600
    receipt = json.loads(files.receipt.read_text())
    assert receipt["state"] == "installed"
    assert (files.hooks / receipt["backup"]).read_bytes() == before
    snapshots = {p: (p.read_bytes(), p.stat().st_mtime_ns) for p in files.hooks.iterdir()}
    settings_mtime = files.settings.stat().st_mtime_ns
    assert files.install(confirmed=True) == HookResult("installed")
    assert snapshots == {p: (p.read_bytes(), p.stat().st_mtime_ns) for p in files.hooks.iterdir()}
    assert files.settings.stat().st_mtime_ns == settings_mtime


def test_custom_conflict_and_explicit_replacement_restore_only_field(files: Files) -> None:
    original = {"statusLine": {"type": "command", "command": "never execute this"}, "old": 1}
    write(files.settings, original)
    before = files.settings.read_bytes()
    assert files.install(confirmed=True).state == "conflict"
    assert files.settings.read_bytes() == before and not files.hooks.exists()
    assert files.install(confirmed=True, replace_statusline=True).state == "installed"
    current = json.loads(files.settings.read_text())
    current.pop("old")
    write(files.settings, {**current, "later": {"important": True}})
    assert files.uninstall(confirmed=True) == HookResult("uninstalled", True, True)
    assert json.loads(files.settings.read_text()) == {
        "statusLine": original["statusLine"],
        "later": {"important": True},
    }
    assert files.uninstall(confirmed=True) == HookResult("uninstalled")
    assert files.install(confirmed=True, replace_statusline=True).state == "installed"
    assert len(list(files.hooks.glob("*.bak"))) == 2  # Unique backups, never overwritten.


def test_uninstall_refuses_changed_field_and_preserves_later_values(files: Files) -> None:
    assert files.install(confirmed=True).state == "installed"
    changed = {"statusLine": {"type": "command", "command": "new custom"}, "later": 42}
    write(files.settings, changed)
    assert files.uninstall(confirmed=True).state == "conflict"
    assert json.loads(files.settings.read_text()) == changed


def test_uninstall_removes_only_managed_field_and_leaves_shared_script(files: Files) -> None:
    assert files.install(confirmed=True).state == "installed"
    current = json.loads(files.settings.read_text())
    write(files.settings, {**current, "later": 42})
    assert files.uninstall(confirmed=True).state == "uninstalled"
    current.pop("statusLine")
    assert json.loads(files.settings.read_text()) == {**current, "later": 42}
    assert files.script.exists()
    assert not files.cache.exists()


@pytest.mark.parametrize(
    "raw",
    [
        b"[]",
        b"null",
        b"{",
        b'{"x":1,"x":2}',
        b'{"x":{"a":1,"a":2}}',
        b'{"x":NaN}',
        b"\xff",
        b"x" * (fs.MAX_BYTES + 1),
    ],
)
def test_invalid_json_has_no_mutation(files: Files, raw: bytes) -> None:
    files.settings.write_bytes(raw)
    assert files.install(confirmed=True).state == "invalid"
    assert files.settings.read_bytes() == raw and not files.hooks.exists()


@pytest.mark.parametrize(
    "unsafe",
    ["symlink", "parent_symlink", "directory", "fifo", "hardlink", "writable", "wrong_owner"],
)
def test_unsafe_settings_refused(
    files: Files, unsafe: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    if unsafe in ("symlink", "directory", "fifo"):
        files.settings.unlink()
    if unsafe == "symlink":
        other = files.settings.parent / "other.json"
        write(other, {})
        files.settings.symlink_to(other)
    elif unsafe == "parent_symlink":
        other = files.settings.parent / "redirect"
        other.symlink_to(files.settings.parent, target_is_directory=True)
        files.settings = other / files.settings.name
    elif unsafe == "directory":
        files.settings.mkdir()
    elif unsafe == "fifo":
        os.mkfifo(files.settings, 0o600)
    elif unsafe == "hardlink":
        os.link(files.settings, files.settings.parent / "linked")
    elif unsafe == "writable":
        files.settings.chmod(0o666)
    elif unsafe == "wrong_owner":
        uid = os.getuid()
        monkeypatch.setattr(fs.os, "getuid", lambda: uid + 1)
    assert files.install(confirmed=True).state in ("invalid", "unreadable")
    assert not files.hooks.exists()


@pytest.mark.parametrize(
    "unsafe",
    [
        "symlink",
        "writable",
        "modified",
        "cache_symlink",
        "cache_writable",
        "backup_tamper",
        "receipt_tamper",
    ],
)
def test_unsafe_installer_artifacts_not_overwritten(files: Files, unsafe: str) -> None:
    assert files.install(confirmed=True).state == "installed"
    settings = files.settings.read_bytes()
    if unsafe == "symlink":
        files.script.unlink()
        files.script.symlink_to(files.settings)
    elif unsafe == "writable":
        files.hooks.chmod(0o755)
    elif unsafe == "modified":
        files.script.write_text("modified executable")
    elif unsafe == "cache_symlink":
        files.cache.symlink_to(files.settings)
    elif unsafe == "cache_writable":
        write(files.cache, {})
        files.cache.chmod(0o644)
    elif unsafe == "backup_tamper":
        backup = files.hooks / json.loads(files.receipt.read_text())["backup"]
        backup.write_text('{"tampered":true}')
    elif unsafe == "receipt_tamper":
        files.receipt.write_text('{"private-marker":"grant=do-not-print"}')
    result = files.install(confirmed=True)
    assert result.state in ("conflict", "invalid", "unreadable")
    assert not result.changed and files.settings.read_bytes() == settings
    assert "private-marker" not in repr(result) and "grant" not in repr(result)


def test_cache_isolation_across_sources_and_shared_resource(files: Files) -> None:
    assert files.install(confirmed=True).state == "installed"
    second = files.settings.parent / "source2.json"
    write(second, {})
    assert (
        install_hook(second, files.cache, confirmed=True, hooks_dir=files.hooks).state == "conflict"
    )
    second_cache = files.cache.parent / "cache2.json"
    assert (
        install_hook(second, second_cache, confirmed=True, hooks_dir=files.hooks).state
        == "installed"
    )
    assert (
        json.loads(second.read_text())["statusLine"]["command"]
        != json.loads(files.settings.read_text())["statusLine"]["command"]
    )
    assert len(list(files.hooks.glob(HOOK_FILENAME))) == 1


@pytest.mark.parametrize("direction", ["settings_is_cache", "cache_is_settings"])
def test_source_cache_cannot_overwrite_another_sources_settings(
    files: Files, direction: str
) -> None:
    assert files.install(confirmed=True).state == "installed"
    second = files.settings.parent / "source2.json"
    write(second, {})
    settings, cache = (
        (files.cache, second) if direction == "settings_is_cache" else (second, files.settings)
    )
    if direction == "settings_is_cache":
        write(settings, {})
    assert install_hook(settings, cache, confirmed=True, hooks_dir=files.hooks).state == "conflict"


@pytest.mark.parametrize("collision", ["settings", "script", "receipt", "lock"])
def test_cache_must_not_alias_installer_files(files: Files, collision: str) -> None:
    cache = {
        "settings": files.settings,
        "script": files.script,
        "receipt": files.receipt,
        "lock": files.settings.parent / ("." + files.receipt.name + ".lock"),
    }[collision]
    assert (
        install_hook(files.settings, cache, confirmed=True, hooks_dir=files.hooks).state
        == "invalid"
    )
    assert not files.hooks.exists()


def test_existing_settings_cache_alias_on_case_insensitive_filesystem(files: Files) -> None:
    alias = files.settings.with_name(files.settings.name.upper())
    if not alias.exists():
        pytest.skip("case-sensitive filesystem")
    before = files.settings.read_bytes()
    assert (
        install_hook(files.settings, alias, confirmed=True, hooks_dir=files.hooks).state
        == "invalid"
    )
    assert files.settings.read_bytes() == before and not files.script.exists()


def test_lock_contention_is_bounded_and_does_not_touch_settings(files: Files) -> None:
    files.hooks.mkdir(mode=0o700)
    lock = files.hooks / ".hook-install.lock"
    before = files.settings.read_bytes()
    with lock.open("wb") as stream:
        lock.chmod(0o600)
        fcntl.flock(stream.fileno(), fcntl.LOCK_EX)
        start = time.monotonic()
        assert files.install(confirmed=True).state == "busy"
        assert time.monotonic() - start < 1
    assert files.settings.read_bytes() == before and not files.script.exists()


def test_concurrent_edit_before_rename_is_not_rolled_back(
    files: Files, monkeypatch: pytest.MonkeyPatch
) -> None:
    original_atomic = fs.atomic
    newer = {"later": "important", "statusLine": {"type": "command", "command": "mine"}}

    def race(path: Path, *args: Any, **kwargs: Any) -> None:
        if path == files.settings:
            write(files.settings, newer)
        original_atomic(path, *args, **kwargs)

    monkeypatch.setattr(fs, "atomic", race)
    assert files.install(confirmed=True).state == "concurrent_edit"
    assert json.loads(files.settings.read_text()) == newer
    assert not list(files.hooks.glob("*.tmp"))


def test_directory_replacement_before_rename_is_rejected(
    files: Files, monkeypatch: pytest.MonkeyPatch
) -> None:
    original_atomic = fs.atomic
    real = fs.directory

    def replace_directory(path: Path, *args: Any, **kwargs: Any) -> None:
        if path == files.settings:
            # Redirecting an ancestor cannot redirect the pinned file descriptor.
            files.settings.parent.rename(files.settings.parent.with_name("moved-owner"))
            files.settings.parent.mkdir(mode=0o700)
            write(files.settings, {"new_owner": True})
        original_atomic(path, *args, **kwargs)

    # Installer resources/cache are siblings; rename only at final settings commit.
    monkeypatch.setattr(fs, "atomic", replace_directory)
    assert files.install(confirmed=True).state == "concurrent_edit"
    assert json.loads(files.settings.read_text()) == {"new_owner": True}
    assert fs.directory is real


@pytest.mark.parametrize("rollback_failure", [False, True])
def test_finalization_failure_rolls_back_only_field(
    files: Files, monkeypatch: pytest.MonkeyPatch, rollback_failure: bool
) -> None:
    original_atomic = fs.atomic
    finalization_failed = False

    def fail(path: Path, fd: int, raw: bytes, *args: Any, **kwargs: Any) -> None:
        nonlocal finalization_failed
        if path == files.receipt and json.loads(raw)["state"] == "installed":
            finalization_failed = True
            current = json.loads(files.settings.read_text())
            write(files.settings, {**current, "later": {"retained": True}})
            raise OSError("bearer=secret/grant/private/path")
        if finalization_failed and rollback_failure and path == files.settings:
            raise OSError("cookie=secret")
        original_atomic(path, fd, raw, *args, **kwargs)

    monkeypatch.setattr(fs, "atomic", fail)
    result = files.install(confirmed=True)
    assert result.state == ("recovery_required" if rollback_failure else "unreadable")
    assert result.changed
    body = json.loads(files.settings.read_text())
    assert body["later"] == {"retained": True}
    assert ("statusLine" in body) is rollback_failure
    assert "secret" not in repr(result)
    monkeypatch.setattr(fs, "atomic", original_atomic)
    # A prepared receipt is recoverable without replaying a full backup document.
    assert files.uninstall(confirmed=True).state == "uninstalled"
    assert json.loads(files.settings.read_text())["later"] == {"retained": True}


def test_uninstall_directory_fsync_failure_reports_actual_change(
    files: Files, monkeypatch: pytest.MonkeyPatch
) -> None:
    assert files.install(confirmed=True).state == "installed"
    real_fsync = os.fsync
    failed = False

    def fail(fd: int) -> None:
        nonlocal failed
        if not failed and "statusLine" not in json.loads(files.settings.read_text()):
            failed = True
            raise OSError("private-marker")
        real_fsync(fd)

    monkeypatch.setattr(fs.os, "fsync", fail)
    assert files.uninstall(confirmed=True) == HookResult("unreadable", True, True)
    assert "statusLine" not in json.loads(files.settings.read_text())
    assert files.uninstall(confirmed=True).state == "uninstalled"


def test_unowned_managed_looking_command_not_adopted(files: Files) -> None:
    write(
        files.settings,
        {
            "statusLine": {
                "type": "command",
                "command": managed_command(files.script, files.cache, files.python),
            }
        },
    )
    assert files.install(confirmed=True).state == "conflict"
    assert not files.script.exists()


def test_resource_validation_failure_has_no_owner_file_write(
    files: Files, monkeypatch: pytest.MonkeyPatch
) -> None:
    def fail() -> bytes:
        raise fs.HookFileError("resource_invalid")

    monkeypatch.setattr("relaydeck.usage.hook_install.hook_bytes", fail)
    assert files.install(confirmed=True).state == "resource_invalid"
    assert not files.hooks.exists()


def test_upgrade_without_a_receipt_fails_closed_and_writes_nothing(
    files: Files, monkeypatch: pytest.MonkeyPatch
) -> None:
    files.hooks.mkdir(mode=0o700)
    files.script.write_bytes(b"#!/bin/sh\n# older hook\n")
    files.script.chmod(0o600)
    monkeypatch.setattr("relaydeck.usage.hook_install._upgradeable", lambda *_a, **_k: True)
    settings_before = files.settings.read_bytes()
    result = files.install(confirmed=True)
    assert result.state == "invalid" and not result.changed
    assert files.script.read_bytes() == b"#!/bin/sh\n# older hook\n"
    assert files.settings.read_bytes() == settings_before
