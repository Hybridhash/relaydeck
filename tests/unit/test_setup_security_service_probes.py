"""Setup boundaries use only temporary files and intercepted subprocesses."""

from __future__ import annotations

import fcntl
import os
import plistlib
import stat
import subprocess
import tomllib
from pathlib import Path
from typing import Any

import pytest

from relaydeck.companion import detect, prereqs, service, tailscale


def _install(home: Path) -> service.Outcome:
    return service.install(
        executable=Path("/synthetic/relaydeck"),
        home=home,
        uid=os.getuid(),
        yes=True,
        load=False,
        path_env="/usr/bin:/bin",
    )


def test_service_does_not_follow_the_old_predictable_temporary_symlink(tmp_path: Path) -> None:
    target = service.agent_path(tmp_path)
    target.parent.parent.mkdir(mode=0o700)
    target.parent.mkdir(mode=0o700)
    victim = tmp_path / "unrelated"
    victim.write_bytes(b"keep")
    target.with_name(target.name + ".tmp").symlink_to(victim)
    result = _install(tmp_path)
    assert victim.read_bytes() == b"keep"
    assert result.changed and result.exit_code == 0
    assert not target.is_symlink()
    assert plistlib.loads(target.read_bytes())["Label"] == service.LABEL


@pytest.mark.parametrize(
    "unsafe", ["parent-link", "writable-parent", "plist-link", "hardlink", "writable-plist", "fifo"]
)
def test_service_refuses_unsafe_existing_paths(tmp_path: Path, unsafe: str) -> None:
    home = tmp_path / "home"
    target = service.agent_path(home)
    home.mkdir(mode=0o700)
    target.parent.parent.mkdir(mode=0o700)
    target.parent.mkdir(mode=0o700)
    victim = tmp_path / "unrelated"
    victim.write_bytes(b"keep")
    victim.chmod(0o600)
    if unsafe == "parent-link":
        target.parent.rmdir()
        target.parent.symlink_to(tmp_path, target_is_directory=True)
    elif unsafe == "writable-parent":
        target.parent.chmod(0o777)
    elif unsafe == "plist-link":
        target.symlink_to(victim)
    elif unsafe == "hardlink":
        os.link(victim, target)
    elif unsafe == "writable-plist":
        target.write_bytes(b"old")
        target.chmod(0o666)
    else:
        os.mkfifo(target)
    before = victim.read_bytes()
    result = _install(home)
    assert result.exit_code == 1 and not result.changed
    assert victim.read_bytes() == before


@pytest.mark.parametrize("program", ["uv", "herdr", "tailscale", "detect"])
def test_probes_never_run_a_candidate_in_a_writable_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, program: str
) -> None:
    folder = tmp_path / "unsafe"
    folder.mkdir(mode=0o777)
    folder.chmod(0o777)
    name = "herdr" if program == "detect" else program
    binary = folder / name
    binary.write_text("#!/bin/sh\nexit 0\n")
    binary.chmod(0o755)
    monkeypatch.setenv("PATH", str(folder))
    monkeypatch.setattr(tailscale, "CANDIDATES", ("tailscale",))
    monkeypatch.setattr(tailscale, "APP_BUNDLE_CLI", str(tmp_path / "absent"))
    calls: list[Any] = []

    def run(*args: Any, **kwargs: Any) -> subprocess.CompletedProcess[str]:
        calls.append((args, kwargs))
        return subprocess.CompletedProcess(args[0], 0, "", "")

    monkeypatch.setattr(subprocess, "run", run)
    if program == "detect":
        assert detect.herdr_status_text([str(folder)]) is None
    elif program == "tailscale":
        with pytest.raises((OSError, tailscale.TailscaleError)):
            tailscale.default_runner(["status", "--json"])
    else:
        with pytest.raises(OSError):
            prereqs._run_fixed(program, ["--version"])
    assert calls == []


def test_plugin_entries_start_from_the_shell_launcher_not_a_path_selected_python() -> None:
    manifest = Path(__file__).resolve().parents[2] / "plugin" / "herdr-plugin.toml"
    doc = tomllib.loads(manifest.read_text())
    commands = [item["command"] for key in ("build", "actions", "panes") for item in doc[key]]
    assert all(command[:2] == ["/bin/sh", "bin/relaydeck-plugin.sh"] for command in commands)


def test_service_exclusive_nofollow_pinned_write_and_sync_order(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    opened = service.os.open
    replaced = service.os.replace
    synced = service.os.fsync
    events: list[str] = []

    def open_file(path: Any, flags: int, *args: Any, **kwargs: Any) -> int:
        if str(path).endswith(".tmp"):
            assert flags & os.O_EXCL and flags & os.O_NOFOLLOW
            assert kwargs["dir_fd"] >= 0
        return opened(path, flags, *args, **kwargs)

    def replace(src: Any, dst: Any, **kwargs: Any) -> None:
        assert kwargs["src_dir_fd"] == kwargs["dst_dir_fd"]
        assert Path(src).name == src and Path(dst).name == dst
        events.append("rename")
        replaced(src, dst, **kwargs)

    def sync(fd: int) -> None:
        events.append("directory" if stat.S_ISDIR(os.fstat(fd).st_mode) else "file")
        synced(fd)

    monkeypatch.setattr(service.os, "open", open_file)
    monkeypatch.setattr(service.os, "replace", replace)
    monkeypatch.setattr(service.os, "fsync", sync)
    result = _install(tmp_path)
    assert result.changed and result.exit_code == 0
    assert events == ["file", "rename", "directory"]
    target = service.agent_path(tmp_path)
    assert target.stat().st_mode & 0o777 == 0o600
    assert target.parent.stat().st_mode & 0o777 == 0o700
    assert service.log_dir(tmp_path).stat().st_mode & 0o777 == 0o700
    assert (service.log_dir(tmp_path) / "companion.out.log").stat().st_mode & 0o777 == 0o600
    assert not list(target.parent.glob("*.tmp"))


@pytest.mark.parametrize("unsafe", ["lock-link", "lock-busy", "log-link"])
def test_service_refuses_unsafe_or_busy_support_paths(tmp_path: Path, unsafe: str) -> None:
    assert _install(tmp_path).exit_code == 0
    target = service.agent_path(tmp_path)
    before = target.read_bytes()
    victim = tmp_path / "victim"
    victim.write_bytes(b"keep")
    lock = target.parent / f".{service.LABEL}.lock"
    fd = None
    if unsafe == "lock-link":
        lock.unlink()
        lock.symlink_to(victim)
    elif unsafe == "log-link":
        log = service.log_dir(tmp_path) / "companion.out.log"
        log.unlink()
        log.symlink_to(victim)
    else:
        fd = os.open(lock, os.O_RDWR)
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    try:
        result = _install(tmp_path)
    finally:
        if fd is not None:
            os.close(fd)
    assert result.exit_code == 1 and not result.changed
    assert target.read_bytes() == before and victim.read_bytes() == b"keep"


@pytest.mark.parametrize("failure", ["file-sync", "directory-sync", "changed-target"])
def test_service_commit_failure_and_replacement_race(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failure: str
) -> None:
    assert _install(tmp_path).exit_code == 0
    target = service.agent_path(tmp_path)
    before = target.read_bytes()
    victim = tmp_path / "victim"
    victim.write_bytes(b"keep")
    synced = service.os.fsync

    def sync(fd: int) -> None:
        is_dir = stat.S_ISDIR(os.fstat(fd).st_mode)
        if failure == "directory-sync" and is_dir or failure == "file-sync" and not is_dir:
            raise OSError("synthetic sync failure")
        if failure == "changed-target" and not is_dir:
            target.unlink()
            target.symlink_to(victim)
        synced(fd)

    monkeypatch.setattr(service.os, "fsync", sync)
    result = _install(tmp_path)
    assert result.exit_code == 1
    assert result.changed is (failure == "directory-sync")
    assert victim.read_bytes() == b"keep"
    if failure != "changed-target":
        assert target.read_bytes() == before
    else:
        assert target.is_symlink()
    assert not list(target.parent.glob("*.tmp"))
