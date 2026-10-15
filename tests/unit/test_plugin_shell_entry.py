"""The plugin's /bin/sh entry: no Python is started before a validated installation exists.

Real entry commands run with an empty PATH and a temporary HOME; fake executables only record
their arguments. Nothing is installed, no network, no real home or Herdr.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

PLUGIN = Path(__file__).resolve().parents[2] / "plugin"
ENTRY = PLUGIN / "bin" / "relaydeck-plugin.sh"
CONTEXT = "CONTEXT-MARKER-91c2"


@pytest.fixture(autouse=True)
def _private_umask() -> object:
    """Directories the tests create must not come out group-writable under a 002 umask."""
    previous = os.umask(0o022)
    yield
    os.umask(previous)


def _run(
    home: Path,
    *args: str,
    env_extra: dict[str, str] | None = None,
    stdin: str = "\n",
) -> subprocess.CompletedProcess[str]:
    env = {"HOME": str(home), "PATH": ""}
    env.update(env_extra or {})
    return subprocess.run(
        ["/bin/sh", "bin/relaydeck-plugin.sh", *args],
        env=env,
        input=stdin,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
        cwd=str(PLUGIN),
    )


def _home(tmp_path: Path) -> Path:
    home = tmp_path / "home"
    home.mkdir()
    home.chmod(0o755)
    return home


def _fake_python(home: Path, record: Path, *, mode: int = 0o755) -> Path:
    """A fake tool interpreter at the fixed candidate location that records how it is run."""
    real = home / ".local/share/uv/python/cpython-3.13/bin/python3.13"
    real.parent.mkdir(parents=True)
    real.write_text(
        "#!/bin/sh\n"
        'for arg in "$@"; do printf "arg=%s\\n" "$arg" >> "$RECORD"; done\n'
        'printf "ctx=%s\\n" "${HERDR_PLUGIN_CONTEXT_JSON-}" >> "$RECORD"\n'
        'printf "path=%s\\n" "$PATH" >> "$RECORD"\n',
        encoding="utf-8",
    )
    real.chmod(mode)
    link = home / ".local/share/uv/tools/relaydeck/bin/python"
    link.parent.mkdir(parents=True)
    link.symlink_to(real)
    return link


def _lines(record: Path) -> list[str]:
    return record.read_text(encoding="utf-8").splitlines() if record.exists() else []


def test_the_shell_entry_never_names_python_or_uv_or_the_path() -> None:
    source = ENTRY.read_text(encoding="utf-8")
    assert source.startswith("#!/bin/sh\n")
    assert "set -eu" in source and "eval " not in source
    executable_lines = [
        line for line in source.splitlines() if line.strip() and not line.lstrip().startswith("#")
    ]
    assert not any("/usr/bin/python" in line or "python3" in line for line in executable_lines)
    assert not any(line.lstrip().startswith("uv ") for line in executable_lines)


def test_build_without_an_installation_is_one_line_exit_zero(tmp_path: Path) -> None:
    result = _run(_home(tmp_path), "build")
    assert result.returncode == 0
    assert result.stdout.strip().count("\n") == 0
    assert "not installed yet" in result.stdout
    assert result.stderr == ""


def test_the_setup_popup_prints_the_install_steps_and_waits_for_a_key(tmp_path: Path) -> None:
    result = _run(_home(tmp_path), "setup", stdin="\n")
    assert result.returncode == 3
    assert "uv tool install --managed-python --python 3.13" in result.stdout
    assert "relaydeck[companion] @ git+https://github.com/Hybridhash/relaydeck@" in result.stdout
    assert "Nothing was installed by this plugin." in result.stdout
    assert "Press Enter to close." in result.stdout


@pytest.mark.parametrize("entry", ["pair", "status"])
def test_the_other_popups_without_an_installation_show_the_same_steps(
    tmp_path: Path, entry: str
) -> None:
    result = _run(_home(tmp_path), entry)
    assert result.returncode == 3 and "relaydeck setup" in result.stdout


@pytest.mark.parametrize("action", ["setup", "pair", "status"])
def test_actions_without_an_installation_only_open_the_setup_popup(
    tmp_path: Path, action: str
) -> None:
    home = _home(tmp_path)
    record = tmp_path / "herdr.txt"
    herdr = tmp_path / "bin" / "herdr"
    herdr.parent.mkdir(mode=0o755)
    herdr.write_text('#!/bin/sh\nfor a in "$@"; do printf "%s\\n" "$a" >> "$RECORD"; done\n')
    herdr.chmod(0o755)
    result = _run(
        home,
        "open",
        action,
        env_extra={"HERDR_BIN_PATH": str(herdr), "RECORD": str(record)},
    )
    assert result.returncode == 0
    assert _lines(record) == [
        "plugin",
        "pane",
        "open",
        "--plugin",
        "relaydeck",
        "--entrypoint",
        "setup",
    ]


def test_an_action_without_herdr_runs_nothing(tmp_path: Path) -> None:
    result = _run(_home(tmp_path), "open", "pair")
    assert result.returncode == 3
    assert "Nothing was run." in result.stdout


def test_an_unsafe_herdr_binary_is_not_executed(tmp_path: Path) -> None:
    record = tmp_path / "herdr.txt"
    herdr = tmp_path / "herdr"
    herdr.write_text('#!/bin/sh\necho ran >> "$RECORD"\n')
    herdr.chmod(0o775)  # group-writable
    result = _run(
        _home(tmp_path),
        "open",
        "setup",
        env_extra={"HERDR_BIN_PATH": str(herdr), "RECORD": str(record)},
    )
    assert result.returncode == 3 and not record.exists()


def test_a_validated_installation_execs_the_launcher_with_the_pinned_interpreter(
    tmp_path: Path,
) -> None:
    home = _home(tmp_path)
    record = tmp_path / "record.txt"
    _fake_python(home, record)
    result = _run(
        home,
        "open",
        "pair",
        env_extra={
            "RECORD": str(record),
            "HERDR_PLUGIN_CONTEXT_JSON": CONTEXT,
            "PATH": "/usr/bin:/bin",
        },
    )
    assert result.returncode == 0
    lines = _lines(record)
    assert lines[0] == "arg=-I"
    assert lines[1] == f"arg={(PLUGIN / 'bin').resolve()}/relaydeck-plugin"
    assert lines[2:5] == ["arg=open", "arg=pair", "ctx="]
    assert CONTEXT not in "\n".join(lines)
    assert lines[5] == "path=/usr/bin:/bin"  # the caller's PATH is handed on, not the empty one


def test_every_mode_is_forwarded_to_the_launcher(tmp_path: Path) -> None:
    home = _home(tmp_path)
    record = tmp_path / "record.txt"
    _fake_python(home, record)
    for mode in ("build", "setup", "pair", "status"):
        record.unlink(missing_ok=True)
        result = _run(home, mode, env_extra={"RECORD": str(record)})
        assert result.returncode == 0
        assert _lines(record)[2] == f"arg={mode}"


def test_a_group_writable_interpreter_is_refused_and_never_run(tmp_path: Path) -> None:
    home = _home(tmp_path)
    record = tmp_path / "record.txt"
    _fake_python(home, record, mode=0o775)
    result = _run(home, "setup", env_extra={"RECORD": str(record)})
    assert result.returncode == 3 and not record.exists()
    assert "not installed" in result.stdout


def test_a_link_into_a_world_writable_directory_is_refused(tmp_path: Path) -> None:
    home = _home(tmp_path)
    record = tmp_path / "record.txt"
    open_dir = tmp_path / "open"
    open_dir.mkdir()
    open_dir.chmod(0o777)
    real = open_dir / "python"
    real.write_text('#!/bin/sh\necho ran >> "$RECORD"\n')
    real.chmod(0o755)
    link = home / ".local/share/uv/tools/relaydeck/bin/python"
    link.parent.mkdir(parents=True)
    link.symlink_to(real)
    result = _run(home, "setup", env_extra={"RECORD": str(record)})
    assert result.returncode == 3 and not record.exists()


def test_a_world_writable_ancestor_is_refused(tmp_path: Path) -> None:
    home = _home(tmp_path)
    record = tmp_path / "record.txt"
    _fake_python(home, record)
    (home / ".local/share/uv/tools").chmod(0o777)
    result = _run(home, "setup", env_extra={"RECORD": str(record)})
    assert result.returncode == 3 and not record.exists()


def test_a_relative_home_is_not_trusted(tmp_path: Path) -> None:
    result = subprocess.run(
        ["/bin/sh", "bin/relaydeck-plugin.sh", "build"],
        env={"HOME": "relative/home", "PATH": ""},
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
        cwd=str(PLUGIN),
    )
    assert result.returncode == 0 and "not installed yet" in result.stdout


def test_usage_errors_exit_two(tmp_path: Path) -> None:
    home = _home(tmp_path)
    for args in ([], ["frobnicate"], ["open"], ["open", "frob"], ["build", "extra"]):
        assert _run(home, *args).returncode == 2, args


def test_the_entry_uses_only_absolute_tools() -> None:
    source = ENTRY.read_text(encoding="utf-8")
    assert os.path.exists("/usr/bin/find") and os.path.exists("/usr/bin/readlink")
    assert "/usr/bin/find" in source and "/usr/bin/readlink" in source
