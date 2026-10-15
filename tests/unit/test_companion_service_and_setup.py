"""the LaunchAgent plist, the `setup` checks and the tailnet port rules.

Every launchctl and tailscale call goes through an injected fake runner; nothing real runs.
"""

from __future__ import annotations

import dataclasses
import json
import os
import plistlib
from pathlib import Path
from typing import Any

import pytest

from relaydeck.companion import service as agent
from relaydeck.companion import setup as setup_mod
from relaydeck.companion import tailscale
from tests.support.companion.asgi_companion import config_file


class _Runner:
    def __init__(self, status: dict[str, object] | None = None) -> None:
        self.calls: list[list[str]] = []
        self.status = status or {}

    def __call__(self, args: Any) -> Any:
        argv = list(args)
        self.calls.append(argv)
        if argv[:3] == ["serve", "status", "--json"]:
            import subprocess

            return subprocess.CompletedProcess(argv, 0, json.dumps(self.status), "")
        import subprocess

        return subprocess.CompletedProcess(argv, 0, "", "")


# -- LaunchAgent ----------------------------------------------------------------


def test_plist_runs_start_with_connect_herdr_and_has_no_secrets(tmp_path: Path) -> None:
    doc = agent.plist_document(
        Path("/opt/relaydeck/bin/relaydeck"), home=tmp_path, path_env="/usr/bin"
    )
    assert doc["Label"] == "dev.relaydeck.companion"
    assert doc["ProgramArguments"] == ["/opt/relaydeck/bin/relaydeck", "start", "--connect-herdr"]
    assert doc["RunAtLoad"] is True and doc["KeepAlive"] is True
    assert doc["ThrottleInterval"] == 30
    assert doc["EnvironmentVariables"] == {"PATH": "/usr/bin"}
    assert str(tmp_path / "Library" / "Logs" / "RelayDeck") in doc["StandardOutPath"]
    text = agent.plist_bytes(
        Path("/opt/relaydeck/bin/relaydeck"), home=tmp_path, path_env="/usr/bin"
    ).decode()
    assert "secret" not in text.lower() and "token" not in text.lower()
    assert plistlib.loads(text.encode()) == doc


def test_install_needs_yes_and_never_runs_launchctl_without_load(tmp_path: Path) -> None:
    runner = _Runner()
    dry = agent.install(
        executable=Path("/x/relaydeck"),
        home=tmp_path,
        uid=501,
        yes=False,
        load=False,
        runner=runner,
    )
    assert not agent.agent_path(tmp_path).exists() and not dry.changed
    written = agent.install(
        executable=Path("/x/relaydeck"),
        home=tmp_path,
        uid=501,
        yes=True,
        load=False,
        path_env="/usr/bin",
        runner=runner,
    )
    assert written.changed and agent.agent_path(tmp_path).is_file()
    assert oct(os.stat(agent.agent_path(tmp_path)).st_mode & 0o777) == "0o600"
    assert runner.calls == []  # no launchctl without --load


def test_load_runs_bootstrap_only_with_yes_and_load(tmp_path: Path) -> None:
    runner = _Runner()
    agent.install(
        executable=Path("/x/relaydeck"),
        home=tmp_path,
        uid=501,
        yes=True,
        load=True,
        path_env="/usr/bin",
        runner=runner,
    )
    assert runner.calls == [["launchctl", "bootstrap", "gui/501", str(agent.agent_path(tmp_path))]]


def test_uninstall_without_yes_keeps_the_file_and_with_yes_removes_it(tmp_path: Path) -> None:
    runner = _Runner()
    agent.install(
        executable=Path("/x/relaydeck"),
        home=tmp_path,
        uid=501,
        yes=True,
        load=False,
        path_env="/usr/bin",
        runner=runner,
    )
    kept = agent.uninstall(home=tmp_path, uid=501, yes=False, unload=False, runner=runner)
    assert agent.agent_path(tmp_path).exists() and not kept.changed
    removed = agent.uninstall(home=tmp_path, uid=501, yes=True, unload=False, runner=runner)
    assert removed.changed and not agent.agent_path(tmp_path).exists()
    assert runner.calls == []


def test_status_reports_the_program_without_launchctl(tmp_path: Path) -> None:
    runner = _Runner()
    agent.install(
        executable=Path("/x/relaydeck"),
        home=tmp_path,
        uid=501,
        yes=True,
        load=False,
        path_env="/usr/bin",
        runner=runner,
    )
    status = agent.status(tmp_path)
    assert status["installed"] is True and status["program"] == "/x/relaydeck"
    assert runner.calls == []


# -- setup ----------------------------------------------------------------------


def test_setup_creates_the_owner_only_config_from_the_template(tmp_path: Path) -> None:
    path = tmp_path / "cfg" / "companion.toml"
    message = setup_mod.ensure_config(path)
    assert "created" in message and path.is_file()
    assert oct(os.stat(path).st_mode & 0o777) == "0o600"
    assert oct(os.stat(path.parent).st_mode & 0o777) == "0o700"
    assert setup_mod.template_text() in path.read_text()
    # An existing config is never rewritten.
    path.write_text("kept = true\n")
    assert "not changed" in setup_mod.ensure_config(path)
    assert path.read_text() == "kept = true\n"


def test_template_matches_the_example_config() -> None:
    root = Path(__file__).resolve().parents[2]
    assert (root / "examples" / "companion.toml").read_text() == setup_mod.template_text()


@pytest.mark.parametrize(
    ("configured", "used", "public", "expected"),
    [
        (8445, set(), set(), 8445),
        (8445, {8445}, set(), None),
        (8445, {8445, 8446}, set(), None),
        (8445, set(), {8445}, None),  # No automatic fallback for Funnel.
        (8443, set(), set(), None),  # Reserved ports require a config edit.
        (8444, set(), set(), None),
        (8446, {8445}, set(), 8446),  # Explicitly configured alternative is allowed.
        (8445, {8445, 8446, 8447, 8448, 8449}, set(), None),
    ],
)
def test_https_port_choice_uses_only_the_explicit_configured_port(
    configured: int, used: set[int], public: set[int], expected: int | None
) -> None:
    assert setup_mod.choose_https_port(configured, used, public) == expected


_STATUS_RUNNING = {
    "BackendState": "Running",
    "Self": {"DNSName": "mac.example.ts.net."},
    "CurrentTailnet": {"MagicDNSEnabled": True},
    "CertDomains": ["mac.example.ts.net"],
}


def ready_probes(serve_runner: Any) -> Any:
    """Every prerequisite ok: status is synthetic; serve calls go to the runner."""
    import subprocess

    from relaydeck.companion import prereqs

    def tailscale_run(args: Any) -> Any:
        argv = list(args)
        if argv[:2] == ["status", "--json"]:
            return subprocess.CompletedProcess(argv, 0, json.dumps(_STATUS_RUNNING), "")
        return serve_runner(argv)

    def herdr(args: Any) -> Any:
        body = {"running": True} if list(args)[:2] == ["status", "server"] else None
        text = json.dumps(body) if body else "herdr 0.9.1"
        return subprocess.CompletedProcess(list(args), 0, text, "")

    async def ping(socket: str) -> None:
        return None

    return prereqs.Probes(
        tailscale=tailscale_run,
        find_tailscale=lambda: "/synthetic/tailscale",
        which=lambda name: f"/synthetic/{name}",
        herdr=herdr,
        uv=lambda args: subprocess.CompletedProcess(list(args), 0, "uv 0.12.15\n", ""),
        ping=ping,
        socket_exists=lambda path: True,
        python_version=(3, 13),
    )


def _serve_status(**kwargs: Any) -> dict[str, object]:
    web = {f"mac.example.ts.net:{port}": {"Handlers": {}} for port in kwargs.get("used", [])}
    funnel = {f"mac.example.ts.net:{port}": True for port in kwargs.get("public", [])}
    return {"Web": web, "AllowFunnel": funnel}


def test_setup_prints_the_exact_serve_command_and_applies_only_with_confirmation(
    tmp_path: Path,
) -> None:
    config = config_file(tmp_path, advertised_origin="https://mac.example.ts.net")
    runner = _Runner(_serve_status(used=[8446]))
    report = setup_mod.run_setup(
        config_path=config,
        assume_yes=False,
        confirm=lambda _prompt: False,
        runner=runner,
        probes=ready_probes(runner),
    )
    text = "\n".join(report.lines)
    assert "tailscale serve --https=8445 --bg http://127.0.0.1:8125" in text
    assert "Not applied" in text
    assert not [c for c in runner.calls if "--bg" in c]

    runner_yes = _Runner(_serve_status(used=[8446]))
    setup_mod.run_setup(
        config_path=config,
        assume_yes=True,
        confirm=lambda _p: False,
        runner=runner_yes,
        probes=ready_probes(runner_yes),
    )
    assert ["serve", "--bg", "--https=8445", "http://127.0.0.1:8125"] in runner_yes.calls


def test_setup_refuses_nothing_free_and_changes_nothing(tmp_path: Path) -> None:
    config = config_file(tmp_path, advertised_origin="https://mac.example.ts.net")
    runner = _Runner(_serve_status(used=[8445, 8446, 8447, 8448, 8449]))
    report = setup_mod.run_setup(
        config_path=config,
        assume_yes=True,
        confirm=lambda _p: True,
        runner=runner,
        probes=ready_probes(runner),
    )
    assert report.exit_code == 1
    assert "Nothing was written" in "\n".join(report.lines)
    assert not [c for c in runner.calls if c[:1] == ["serve"] and "--bg" in c]


def test_setup_never_applies_a_mapping_that_already_exists(tmp_path: Path) -> None:
    config = config_file(tmp_path, advertised_origin="https://mac.example.ts.net")
    proxy_status = {
        "Web": {"mac.example.ts.net:8445": {"Handlers": {"/": {"Proxy": "http://127.0.0.1:8125"}}}},
        "AllowFunnel": {},
    }
    runner = _Runner(proxy_status)
    report = setup_mod.run_setup(
        config_path=config,
        assume_yes=True,
        confirm=lambda _p: True,
        runner=runner,
        probes=ready_probes(runner),
    )
    assert "already maps" in "\n".join(report.lines)
    assert not [c for c in runner.calls if c[:1] == ["serve"] and "--bg" in c]


def test_setup_reports_a_missing_herdr_socket(tmp_path: Path) -> None:
    config = config_file(tmp_path, advertised_origin="https://mac.example.ts.net")
    probes = ready_probes(_Runner({}))
    probes = dataclasses.replace(probes, socket_exists=lambda path: False)
    report = setup_mod.run_setup(
        config_path=config,
        assume_yes=False,
        confirm=lambda _p: False,
        runner=_Runner({}),
        probes=probes,
    )
    assert report.exit_code == 1
    assert "herdr: not_running" in "\n".join(report.lines)  # the socket is missing: setup stops


def test_tailscale_helpers_read_only_port_sets() -> None:
    doc = {"Web": {"x.ts.net:8445": {}, "x.ts.net:443": {}}}
    assert tailscale.served_https_ports(doc) == {8445, 443}
