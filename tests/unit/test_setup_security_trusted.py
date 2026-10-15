"""Executable trust policy with synthetic paths and intercepted process execution."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path
from typing import Any

import pytest

from relaydeck.companion import detect, prereqs, service, tailscale, trusted


def _binary(path: Path) -> Path:
    path.parent.mkdir(mode=0o700, exist_ok=True)
    path.write_text("#!/bin/sh\nexit 0\n")
    path.chmod(0o755)
    return path


@pytest.mark.parametrize("unsafe", ["original", "target", "intermediate"])
def test_trust_checks_every_symlink_hop_and_both_ancestries(tmp_path: Path, unsafe: str) -> None:
    binary = _binary(tmp_path / "private" / "real")
    links = tmp_path / "links"
    links.mkdir(mode=0o700)
    link = links / "tool"
    middle = tmp_path / "middle"
    middle.mkdir(mode=0o700)
    hop = middle / "hop"
    hop.symlink_to(binary)
    link.symlink_to(hop)
    assert trusted.trusted_executable(str(link)) == str(binary)
    bad = {"original": links, "target": binary.parent, "intermediate": middle}[unsafe]
    bad.chmod(0o777)
    assert trusted.trusted_executable(str(link)) is None


@pytest.mark.parametrize("mode", [0o775, 0o757, 0o644])
def test_executable_permissions_are_checked(tmp_path: Path, mode: int) -> None:
    binary = _binary(tmp_path / "bin" / "tool")
    binary.chmod(mode)
    assert trusted.trusted_executable(str(binary)) is None


@pytest.mark.parametrize("entry", ["file", "directory", "link"])
def test_executable_owner_is_checked(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, entry: str
) -> None:
    binary = _binary(tmp_path / "bin" / "tool")
    link = tmp_path / "link"
    link.symlink_to(binary)
    checked = {"file": binary, "directory": binary.parent, "link": link}[entry]
    original = Path.lstat

    def lstat(path: Path) -> os.stat_result:
        info = original(path)
        if path == checked:
            values = list(info)
            values[4] = os.getuid() + 1
            return os.stat_result(values)
        return info

    monkeypatch.setattr(Path, "lstat", lstat)
    assert trusted.trusted_executable(str(link)) is None


def test_special_files_cycles_and_relative_path_entries_are_refused(tmp_path: Path) -> None:
    fifo = tmp_path / "fifo"
    os.mkfifo(fifo)
    link = tmp_path / "cycle"
    link.symlink_to(link)
    assert trusted.trusted_executable(str(fifo)) is None
    assert trusted.trusted_executable(str(link)) is None
    assert trusted.trusted_executable("tool", directories=["", ".", "relative"]) is None
    assert trusted.trusted_executable("relative/tool") is None
    link.unlink()
    link.symlink_to(".")
    assert trusted.trusted_executable(str(link)) is None


@pytest.mark.parametrize(
    "program,timeout",
    [("uv", 15), ("herdr", 15), ("tailscale", 15), ("detect", 5), ("launchctl", 30)],
)
def test_every_probe_runs_a_resolved_absolute_path_with_a_constrained_environment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, program: str, timeout: int
) -> None:
    name = "herdr" if program == "detect" else program
    binary = _binary(tmp_path / "private" / name)
    links = tmp_path / "bin"
    links.mkdir(mode=0o700)
    (links / name).symlink_to(binary)
    unsafe = tmp_path / "unsafe"
    unsafe.mkdir(mode=0o777)
    unsafe.chmod(0o777)
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("PATH", os.pathsep.join([str(links), str(unsafe), "."]))
    for key in (
        "PYTHONPATH",
        "PYTHONHOME",
        "DYLD_INSERT_LIBRARIES",
        "LD_PRELOAD",
        "BASH_ENV",
        "TOKEN",
        "HERDR_PLUGIN_CONTEXT_JSON",
    ):
        monkeypatch.setenv(key, "must-not-reach-child")
    calls: list[tuple[Any, dict[str, Any]]] = []

    def run(args: Any, **kwargs: Any) -> subprocess.CompletedProcess[str]:
        calls.append((args, kwargs))
        return subprocess.CompletedProcess(args, 0, "synthetic", "")

    monkeypatch.setattr(subprocess, "run", run)
    if program == "detect":
        assert detect.herdr_status_text([str(links)]) == "synthetic"
    elif program == "tailscale":
        tailscale.default_runner(["status", "--json"])
    elif program == "launchctl":

        def resolve(name: str) -> str | None:
            assert name == "/bin/launchctl"
            return trusted.trusted_executable(str(links / "launchctl"))

        monkeypatch.setattr(service, "trusted_executable", resolve)
        assert service.default_runner(["launchctl", "bootstrap", "gui/501", "synthetic"]) == 0
    else:
        prereqs._run_fixed(program, ["--version"])
    assert len(calls) == 1
    args, options = calls[0]
    assert args[0] == str(binary)
    assert options["timeout"] == timeout
    env = options["env"]
    assert env["HOME"] == str(tmp_path)
    assert "must-not-reach-child" not in env.values()
    assert str(unsafe) not in env["PATH"].split(os.pathsep)
    assert "." not in env["PATH"].split(os.pathsep)


def test_launchctl_refusal_never_executes(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(service, "trusted_executable", lambda name: None)

    def forbidden(*args: Any, **kwargs: Any) -> Any:
        pytest.fail("an untrusted launchctl ran")

    monkeypatch.setattr(subprocess, "run", forbidden)
    with pytest.raises(FileNotFoundError):
        service.default_runner(["launchctl", "bootstrap", "gui/501", str(tmp_path)])
