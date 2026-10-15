"""the Herdr plugin manifest and its launcher.

Static checks parse plugin/herdr-plugin.toml with tomllib. Launcher checks run plugin/bin/relaydeck-
plugin
as a child process with a temporary HOME, a minimal PATH (no uv) and fake executables that only
record
their argv. Nothing is installed, linked or published, and no network is used.
"""

from __future__ import annotations

import subprocess
import sys
import tomllib
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
PLUGIN = ROOT / "plugin"
MANIFEST = PLUGIN / "herdr-plugin.toml"
LAUNCHER = PLUGIN / "bin" / "relaydeck-plugin"
VALID_PLACEMENTS = {"overlay", "popup", "split", "tab", "zoomed"}
SHELLS = {"sh", "bash", "zsh", "dash", "ksh", "fish"}
CONTEXT_MARKER = "CONTEXT-MARKER-7f3a"


def _manifest() -> dict:
    return tomllib.loads(MANIFEST.read_text(encoding="utf-8"))


def _commands(manifest: dict) -> list[tuple[str, list]]:
    found = [("build", manifest["build"][0]["command"])]
    found += [(f"action:{item['id']}", item["command"]) for item in manifest["actions"]]
    found += [(f"pane:{item['id']}", item["command"]) for item in manifest["panes"]]
    return found


def test_the_identity_and_platform_match_the_brief() -> None:
    manifest = _manifest()
    assert manifest["id"] == "relaydeck"
    assert manifest["min_herdr_version"] == "0.7.5"
    assert manifest["platforms"] == ["macos"]


def test_every_command_is_an_argv_list_of_strings() -> None:
    for where, command in _commands(_manifest()):
        assert isinstance(command, list) and command, where
        assert all(isinstance(part, str) and part for part in command), where


def test_no_command_names_python_and_every_entry_starts_from_the_shell_launcher() -> None:
    for where, command in _commands(_manifest()):
        assert command[:2] == ["/bin/sh", "bin/relaydeck-plugin.sh"], where
        assert not any("python" in part for part in command), where


def test_no_command_runs_through_a_shell() -> None:
    for where, command in _commands(_manifest()):
        assert not (set(command) & SHELLS), (where, command)
        assert "-c" not in command, where
        for position, part in enumerate(command[:-1]):
            assert not (part in SHELLS and command[position + 1] == "-c"), (where, command)


def test_there_is_no_startup_or_events_section() -> None:
    manifest = _manifest()
    assert "startup" not in manifest
    assert "events" not in manifest


def test_every_pane_is_a_popup_with_a_valid_placement() -> None:
    for pane in _manifest()["panes"]:
        assert pane["placement"] == "popup", pane["id"]
        assert pane["placement"] in VALID_PLACEMENTS


def test_the_ids_are_the_three_actions_and_their_panes() -> None:
    manifest = _manifest()
    assert [item["id"] for item in manifest["actions"]] == ["setup", "pair", "status"]
    assert [item["id"] for item in manifest["panes"]] == ["setup", "pair", "status"]
    titles = {item["id"]: item["title"] for item in manifest["actions"]}
    assert titles == {
        "setup": "RelayDeck: Set up",
        "pair": "RelayDeck: Pair a device",
        "status": "RelayDeck: Status",
    }
    assert all(item["contexts"] == ["global"] for item in manifest["actions"])


def test_each_pane_runs_its_command_in_the_popup_tty() -> None:
    for pane in _manifest()["panes"]:
        command = pane["command"]
        assert command[:2] == ["/bin/sh", "bin/relaydeck-plugin.sh"], pane["id"]
        assert command[-1] == pane["id"], pane["id"]


def test_each_action_only_opens_its_popup_pane_and_never_runs_a_command_directly() -> None:
    """Herdr runs actions without a terminal (an action only opens its pane). Each action
    therefore runs `open <entrypoint>` and nothing else: setup, pair and status run only in the
    panes."""
    for action in _manifest()["actions"]:
        command = action["command"]
        assert command[:2] == ["/bin/sh", "bin/relaydeck-plugin.sh"], action["id"]
        assert command[-2:] == ["open", action["id"]], action["id"]
        assert command[-1] not in {"setup", "pair", "status"} or command[-2] == "open"


def test_the_build_step_only_checks() -> None:
    assert _manifest()["build"][0]["command"][-1] == "build"


def test_the_status_launcher_mode_calls_only_status_and_doctor() -> None:
    source = LAUNCHER.read_text(encoding="utf-8")
    body = source[source.index("def run_status") : source.index("def run_build")]
    assert '"status"' in body and '"doctor"' in body
    for forbidden in ("pairing", "approve", "setup", "--probe-private", "--yes"):
        assert forbidden not in body, forbidden


def test_the_launcher_never_reads_or_forwards_the_context_json() -> None:
    source = LAUNCHER.read_text(encoding="utf-8")
    assert "HERDR_PLUGIN_CONTEXT_JSON" in source  # it is dropped from the child environment
    assert 'os.environ.get("HERDR_PLUGIN_CONTEXT_JSON"' not in source
    assert "--yes" not in source


# -- the launcher, run as a child process with fake executables -----------------------------------


def _fake(path: Path, record: Path) -> None:
    path.write_text(
        "#!/bin/sh\n"
        'for arg in "$@"; do printf "%s\\n" "$arg" >> "$RECORD"; done\n'
        'printf "%s\\n" "--call--" >> "$RECORD"\n'
        'printf "ctx=%s\\n" "${HERDR_PLUGIN_CONTEXT_JSON-}" >> "$RECORD"\n'
        'echo "fake $(basename "$0") ran"\n',
        encoding="utf-8",
    )
    path.chmod(0o755)


def _home(tmp_path: Path) -> tuple[Path, Path]:
    home = tmp_path / "home"
    (home / ".local" / "bin").mkdir(parents=True)
    home.chmod(0o755)
    (home / ".local").chmod(0o755)
    (home / ".local" / "bin").chmod(0o755)
    record = tmp_path / "record.txt"
    return home, record


def _run(
    mode: str,
    home: Path,
    record: Path,
    *,
    context: str | None = None,
    herdr_bin: str | None = None,
    args: list[str] | None = None,
) -> subprocess.CompletedProcess:
    env = {"HOME": str(home), "PATH": "/usr/bin:/bin", "RECORD": str(record)}
    if context is not None:
        env["HERDR_PLUGIN_CONTEXT_JSON"] = context
    if herdr_bin is not None:
        env["HERDR_BIN_PATH"] = herdr_bin
    return subprocess.run(
        [sys.executable, "-I", str(LAUNCHER), mode, *(args or [])],
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
        cwd=str(PLUGIN),
    )


def _calls(record: Path) -> list[list[str]]:
    if not record.exists():
        return []
    calls: list[list[str]] = [[]]
    for line in record.read_text(encoding="utf-8").splitlines():
        if line == "--call--":
            calls.append([])
        elif line.startswith("ctx="):
            continue
        else:
            calls[-1].append(line)
    return [call for call in calls if call]


def test_a_missing_package_prints_both_install_routes_and_stops_nonzero(tmp_path: Path) -> None:
    home, record = _home(tmp_path)
    result = _run("setup", home, record)
    assert result.returncode == 3
    assert 'uv tool install "relaydeck[companion] @ git+' in result.stdout
    assert 'uv tool install ".[companion]"' in result.stdout
    assert "Nothing was installed by this plugin." in result.stdout
    assert not record.exists()


def test_a_group_writable_binary_is_refused(tmp_path: Path) -> None:
    home, record = _home(tmp_path)
    binary = home / ".local" / "bin" / "relaydeck"
    _fake(binary, record)
    binary.chmod(0o775)
    result = _run("setup", home, record)
    assert result.returncode == 3
    assert "group- or world-writable" in result.stdout
    assert not record.exists()


def test_a_symlink_to_a_writable_target_is_refused(tmp_path: Path) -> None:
    home, record = _home(tmp_path)
    target_dir = tmp_path / "open-to-others"
    target_dir.mkdir()
    target_dir.chmod(0o777)
    target = target_dir / "relaydeck-real"
    _fake(target, record)
    (home / ".local" / "bin" / "relaydeck").symlink_to(target)
    result = _run("setup", home, record)
    assert result.returncode == 3
    assert "symbolic link to an unsafe target" in result.stdout
    assert not record.exists()


def test_a_symlink_to_a_private_target_is_accepted(tmp_path: Path) -> None:
    """Control for the refusal above: a symlink to a private target in a private directory runs."""
    home, record = _home(tmp_path)
    target_dir = tmp_path / "private"
    target_dir.mkdir()
    target_dir.chmod(0o755)
    target = target_dir / "relaydeck-real"
    _fake(target, record)
    (home / ".local" / "bin" / "relaydeck").symlink_to(target)
    result = _run("setup", home, record)
    assert result.returncode == 0, result.stdout + result.stderr
    assert record.exists()


def test_the_context_json_never_reaches_argv_or_the_output(tmp_path: Path) -> None:
    home, record = _home(tmp_path)
    _fake(home / ".local" / "bin" / "relaydeck", record)
    _fake(home / ".local" / "bin" / "relaydeck-companion", record)
    context = '{"selection": "' + CONTEXT_MARKER + '"}'
    for mode in ("setup", "pair", "status"):
        result = _run(mode, home, record, context=context)
        assert CONTEXT_MARKER not in result.stdout
        assert CONTEXT_MARKER not in result.stderr
    recorded = record.read_text(encoding="utf-8")
    assert CONTEXT_MARKER not in recorded
    assert "ctx=\n" in recorded  # the child saw an empty context, not the value


def test_status_only_ever_calls_status_then_doctor(tmp_path: Path) -> None:
    home, record = _home(tmp_path)
    _fake(home / ".local" / "bin" / "relaydeck-companion", record)
    result = _run("status", home, record)
    assert result.returncode == 0, result.stdout + result.stderr
    assert _calls(record) == [["status"], ["doctor"]]


def test_pair_opens_a_window_and_never_approves_or_passes_yes(tmp_path: Path) -> None:
    home, record = _home(tmp_path)
    _fake(home / ".local" / "bin" / "relaydeck-companion", record)
    result = _run("pair", home, record)
    assert result.returncode == 0, result.stdout + result.stderr
    assert _calls(record) == [["pairing", "open"]]
    assert "The plugin never approves a device for you." in result.stdout


def test_setup_runs_the_interactive_setup_without_yes(tmp_path: Path) -> None:
    home, record = _home(tmp_path)
    _fake(home / ".local" / "bin" / "relaydeck", record)
    result = _run("setup", home, record)
    assert result.returncode == 0, result.stdout + result.stderr
    assert _calls(record) == [["setup"]]


def test_build_checks_uv_and_installs_nothing(tmp_path: Path) -> None:
    home, record = _home(tmp_path)
    result = _run("build", home, record)
    assert result.returncode == 3
    assert "Nothing was installed by this plugin." in result.stdout
    assert not record.exists()


def test_an_unknown_mode_is_a_usage_error(tmp_path: Path) -> None:
    home, record = _home(tmp_path)
    result = subprocess.run(
        [sys.executable, "-I", str(LAUNCHER), "install"],
        env={"HOME": str(home), "PATH": "/usr/bin:/bin"},
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert result.returncode == 2


# -- the action side: `open <entrypoint>` asks Herdr for the popup pane, nothing else
# -----------------


def _herdr(tmp_path: Path, record: Path) -> Path:
    herdr = tmp_path / "herdr-fake" / "herdr"
    herdr.parent.mkdir()
    herdr.parent.chmod(0o755)
    _fake(herdr, record)
    return herdr


@pytest.mark.parametrize("entrypoint", ["setup", "pair", "status"])
def test_an_action_asks_herdr_to_open_the_popup_pane_and_runs_nothing_else(
    tmp_path: Path, entrypoint: str
) -> None:
    home, record = _home(tmp_path)
    _fake(home / ".local" / "bin" / "relaydeck", record)
    _fake(home / ".local" / "bin" / "relaydeck-companion", record)
    herdr = _herdr(tmp_path, record)
    result = _run("open", home, record, herdr_bin=str(herdr), args=[entrypoint])
    assert result.returncode == 0, result.stdout + result.stderr
    assert _calls(record) == [
        ["plugin", "pane", "open", "--plugin", "relaydeck", "--entrypoint", entrypoint]
    ]


@pytest.mark.parametrize(
    "herdr_bin",
    [None, "relative/herdr", "/nonexistent/path/herdr"],
    ids=["unset", "relative", "missing"],
)
def test_an_open_without_a_valid_herdr_path_prints_guidance_and_exits_nonzero(
    tmp_path: Path, herdr_bin: str | None
) -> None:
    home, record = _home(tmp_path)
    result = _run("open", home, record, herdr_bin=herdr_bin, args=["setup"])
    assert result.returncode == 3
    assert "Nothing was run." in result.stdout
    assert not record.exists()


def test_a_group_writable_herdr_binary_is_refused(tmp_path: Path) -> None:
    home, record = _home(tmp_path)
    herdr = _herdr(tmp_path, record)
    herdr.chmod(0o775)
    result = _run("open", home, record, herdr_bin=str(herdr), args=["pair"])
    assert result.returncode == 3
    assert "HERDR_BIN_PATH is refused" in result.stdout
    assert not record.exists()


def test_a_herdr_binary_in_a_world_writable_directory_is_refused(tmp_path: Path) -> None:
    home, record = _home(tmp_path)
    herdr = _herdr(tmp_path, record)
    herdr.parent.chmod(0o777)
    result = _run("open", home, record, herdr_bin=str(herdr), args=["status"])
    assert result.returncode == 3
    assert not record.exists()


def test_the_context_json_never_reaches_the_herdr_command_or_the_output(tmp_path: Path) -> None:
    home, record = _home(tmp_path)
    herdr = _herdr(tmp_path, record)
    context = '{"selection": "' + CONTEXT_MARKER + '"}'
    result = _run("open", home, record, context=context, herdr_bin=str(herdr), args=["setup"])
    assert result.returncode == 0
    assert CONTEXT_MARKER not in result.stdout + result.stderr
    assert CONTEXT_MARKER not in record.read_text(encoding="utf-8")


def test_open_takes_only_a_known_popup_entrypoint(tmp_path: Path) -> None:
    home, record = _home(tmp_path)
    herdr = _herdr(tmp_path, record)
    result = _run("open", home, record, herdr_bin=str(herdr), args=["install"])
    assert result.returncode == 2
    assert not record.exists()


# Discovery and resolved-path execution --------------------------------------


def _run_with_path(
    mode: str, home: Path, record: Path, path_dirs: list[Path]
) -> subprocess.CompletedProcess:
    env = {
        "HOME": str(home),
        "PATH": ":".join([*(str(d) for d in path_dirs), "/usr/bin", "/bin"]),
        "RECORD": str(record),
    }
    return subprocess.run(
        [sys.executable, "-I", str(LAUNCHER), mode],
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
        cwd=str(PLUGIN),
    )


def _fake_uv(directory: Path, record: Path, mode: int) -> Path:
    directory.mkdir(exist_ok=True)
    directory.chmod(0o755)
    uv = directory / "uv"
    _fake(uv, record)
    uv.chmod(mode)
    return uv


def test_an_unsafe_uv_on_path_is_refused_and_never_run_during_discovery(tmp_path: Path) -> None:
    home, record = _home(tmp_path)
    bin_dir = tmp_path / "pathbin"
    _fake_uv(bin_dir, record, 0o775)  # group-writable
    result = _run_with_path("setup", home, record, [bin_dir])
    assert not record.exists(), "an unvalidated uv was executed"
    assert result.returncode == 3  # nothing installed, so the install guidance follows


def test_an_unsafe_uv_is_refused_by_the_build_check_with_its_reason(tmp_path: Path) -> None:
    home, record = _home(tmp_path)
    bin_dir = tmp_path / "pathbin"
    _fake_uv(bin_dir, record, 0o757)  # world-writable
    result = _run_with_path("build", home, record, [bin_dir])
    assert result.returncode == 3
    assert "refused to run it" in result.stdout
    assert not record.exists()


def test_a_uv_in_a_writable_directory_is_refused(tmp_path: Path) -> None:
    home, record = _home(tmp_path)
    bin_dir = tmp_path / "pathbin"
    _fake_uv(bin_dir, record, 0o755)
    bin_dir.chmod(0o777)
    result = _run_with_path("build", home, record, [bin_dir])
    assert result.returncode == 3 and not record.exists()
    assert "refused to run it" in result.stdout


def test_a_safe_uv_is_still_run_for_discovery(tmp_path: Path) -> None:
    home, record = _home(tmp_path)
    bin_dir = tmp_path / "pathbin"
    _fake_uv(bin_dir, record, 0o755)
    _run_with_path("build", home, record, [bin_dir])
    assert record.exists()  # the validated uv ran (`python find 3.13`)


def test_a_symlink_inside_a_writable_directory_is_refused_even_with_a_private_target(
    tmp_path: Path,
) -> None:
    home, record = _home(tmp_path)
    target_dir = tmp_path / "private"
    target_dir.mkdir()
    target_dir.chmod(0o755)
    target = target_dir / "relaydeck-real"
    _fake(target, record)
    (home / ".local" / "bin").chmod(0o777)  # the link lives in a directory others can write
    (home / ".local" / "bin" / "relaydeck").symlink_to(target)
    result = _run("setup", home, record)
    assert result.returncode == 3
    assert "group- or world-writable" in result.stdout
    assert not record.exists()


def test_a_writable_ancestor_of_the_target_is_refused(tmp_path: Path) -> None:
    home, record = _home(tmp_path)
    outer = tmp_path / "outer"
    inner = outer / "inner"
    inner.mkdir(parents=True)
    outer.chmod(0o777)  # not the target's own directory: its parent
    inner.chmod(0o755)
    target = inner / "relaydeck-real"
    _fake(target, record)
    (home / ".local" / "bin" / "relaydeck").symlink_to(target)
    result = _run("setup", home, record)
    assert result.returncode == 3 and not record.exists()


def test_the_exec_target_is_the_resolved_path_not_the_link(tmp_path: Path) -> None:
    home, record = _home(tmp_path)
    target_dir = tmp_path / "private"
    target_dir.mkdir()
    target_dir.chmod(0o755)
    target = target_dir / "relaydeck-real"
    target.write_text('#!/bin/sh\nprintf "%s\\n" "$0" > "$RECORD"\n', encoding="utf-8")
    target.chmod(0o755)
    (home / ".local" / "bin" / "relaydeck").symlink_to(target)
    result = _run("setup", home, record)
    assert result.returncode == 0, result.stdout + result.stderr
    ran = Path(record.read_text(encoding="utf-8").strip())
    assert ran == Path(target).resolve()
    assert ran.name == "relaydeck-real"  # not the link name
