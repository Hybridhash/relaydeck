"""Setup authorization and port ownership: synthetic profiles/probes only."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from relaydeck import cli
from relaydeck.companion import cli as companion_cli
from relaydeck.companion import setup
from relaydeck.usage import hook_files
from relaydeck.usage.hook_setup import default_hook_path
from tests.support.companion.asgi_companion import config_file
from tests.support.setup.prereqs import Serve, only, probes

TARGET = "http://127.0.0.1:8125"
HOSTPORT = "mac.synthetic.ts.net:8445"


@pytest.fixture(autouse=True)
def _no_detected_clis(monkeypatch: pytest.MonkeyPatch) -> None:
    """the source step asks per detected CLI; these tests cover the hook step only."""
    from relaydeck.companion.detect import DetectReport

    monkeypatch.setattr(setup, "detect_report", lambda _config_path: DetectReport((), "ok", "ok"))


def mapping(target: str = TARGET, *, extra: bool = False) -> dict[str, Any]:
    handlers = {"/": {"Proxy": target}}
    if extra:
        handlers["/other"] = {"Text": "private-marker"}
    return {"TCP": {"8445": {"HTTPS": True}}, "Web": {HOSTPORT: {"Handlers": handlers}}}


CONFLICTS = [
    mapping("http://127.0.0.1:9000/private?grant=secret"),
    mapping(extra=True),
    {"Web": {HOSTPORT: {"Handlers": {"/": {"Path": "/private-marker"}}}}},
    {"Web": {HOSTPORT: {"Handlers": {}}}},
    {"TCP": {"8445": {"TCPForward": "127.0.0.1:9000"}}},
    {"TCP": {"8445": {"HTTPS": True}}},
    {**mapping(), "TCP": {"8445": {"TCPForward": "127.0.0.1:9000"}}},
]


@pytest.mark.parametrize("serve_doc", CONFLICTS)
@pytest.mark.parametrize("assume_yes", [False, True])
def test_conflicting_handlers_stop_before_ensure_config_even_with_yes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, serve_doc: dict[str, Any], assume_yes: bool
) -> None:
    root = tmp_path.resolve()
    config_path = root / "not-created.toml"
    fake = SimpleNamespace(
        herdr_socket=str(root / "socket"),
        tailscale_https_port=8445,
        port=8125,
        local_endpoint=TARGET,
        usage=None,
        agent_kinds=("kind-a",),
    )
    monkeypatch.setattr(setup, "load_config", lambda path, **kwargs: fake)
    created: list[Path] = []
    monkeypatch.setattr(setup, "ensure_config", lambda path: created.append(path) or "created")
    serve = Serve(serve=serve_doc)
    report = setup.run_setup(
        config_path=config_path,
        assume_yes=assume_yes,
        confirm=lambda _: True,
        probes=probes(serve),
        runner=serve,
    )
    assert report.exit_code != 0
    assert created == [] and serve.mutations == [] and not config_path.exists()
    text = "\n".join(report.lines)
    assert "tailscale_https_port" in text
    assert "private-marker" not in text and "grant=secret" not in text


def test_preflight_uses_configured_local_port_and_identical_mapping_is_idempotent(
    tmp_path: Path,
) -> None:
    root = tmp_path.resolve()
    socket = root / "socket"
    socket.touch()
    profile = config_file(root, herdr_socket=str(socket), port=8129)
    serve = Serve(serve=mapping("http://127.0.0.1:8129"))
    report = setup.preflight(profile, probes(serve), serve)
    assert report.ready and only(report, "tailscale_serve").state == "ok"
    before = profile.read_bytes()
    done = setup.run_setup(
        config_path=profile,
        assume_yes=True,
        confirm=lambda _: False,
        probes=probes(serve),
        runner=serve,
    )
    assert done.exit_code == 0 and "already maps" in "\n".join(done.lines)
    assert serve.mutations == [] and profile.read_bytes() == before


def test_only_explicit_configured_port_change_allows_alternative(tmp_path: Path) -> None:
    root = tmp_path.resolve()
    socket = root / "socket"
    socket.touch()
    profile = config_file(root, herdr_socket=str(socket), tailscale_https_port=8446)
    serve = Serve(serve=mapping("http://127.0.0.1:9000"))  # Preserve occupied 8445.
    result = setup.run_setup(
        config_path=profile,
        assume_yes=True,
        confirm=lambda _: False,
        probes=probes(serve),
        runner=serve,
    )
    assert result.exit_code == 0
    assert serve.mutations == [["serve", "--bg", "--https=8446", TARGET]]


def test_mapping_that_changes_during_confirmation_is_never_overwritten(tmp_path: Path) -> None:
    root = tmp_path.resolve()
    socket = root / "socket"
    socket.touch()
    profile = config_file(root, herdr_socket=str(socket))
    serve = Serve()

    def consent(_: str) -> bool:
        serve.serve = mapping("http://127.0.0.1:9000")
        return True

    result = setup.run_setup(
        config_path=profile, assume_yes=False, confirm=consent, probes=probes(serve), runner=serve
    )
    assert result.exit_code != 0 and serve.mutations == []


@pytest.mark.parametrize(
    "document",
    [
        {"TCP": {"8445": None}},
        {"Web": {HOSTPORT: None}},
        {"TCP": {"9" * 5000: {"HTTPS": True}}},
        {"TCP": []},
        {"Foreground": {"opaque": mapping("http://127.0.0.1:9000")}},
    ],
)
def test_malformed_or_foreground_conflict_is_not_treated_as_free(
    tmp_path: Path, document: dict[str, Any]
) -> None:
    root = tmp_path.resolve()
    profile = config_file(root)
    serve = Serve(serve=document)
    assert not setup.preflight(profile, probes(serve), serve).ready
    assert serve.mutations == []


@pytest.fixture
def hook_profile(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Path, Path, Serve]:
    root = tmp_path.resolve()
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: root))
    socket = root / "socket"
    socket.touch()
    profile = config_file(root, herdr_socket=str(socket), agent_kinds=["kind-a"])
    settings = root / "settings.json"
    settings.write_text('{"unrelated":{"keep":"private-marker"}}')
    settings.chmod(0o600)
    with profile.open("a") as out:
        out.write(
            '\n[usage]\ntimezone="UTC"\n[[usage.sources]]\nkind="kind-a"\nformat="jsonl-message-usage-v1"\nlimits_format="json-statusline-limits-v1"\n'
        )
        out.write(f"root={json.dumps(str(root))}\nlimits_settings={json.dumps(str(settings))}\n")
        out.write(f"limits_cache={json.dumps(str(root / 'cache.json'))}\n")
    return profile, settings, Serve()


def run(
    hook_profile: tuple[Path, Path, Serve], *, authorize: bool, answers: list[bool] | None = None
) -> setup.SetupReport:
    profile, _, serve = hook_profile
    responses = iter(answers or [])
    return setup.run_setup(
        config_path=profile,
        assume_yes=True,
        confirm=lambda _: next(responses),
        probes=probes(serve),
        runner=serve,
        install_limits_hook=authorize,
    )


def test_declining_optional_hook_is_documented_skip_and_yes_does_not_authorize_it(
    hook_profile: tuple[Path, Path, Serve],
) -> None:
    _, settings, serve = hook_profile
    before = settings.read_bytes()
    report = run(hook_profile, authorize=False, answers=[False])
    assert report.exit_code == 0 and "skipped" in "\n".join(report.lines)
    assert settings.read_bytes() == before and not default_hook_path().exists()
    assert serve.mutations  # --yes still authorizes Serve alone.


@pytest.mark.parametrize("flag", [False, True])
def test_explicit_hook_flag_or_separate_consent_installs_then_allows_serve(
    hook_profile: tuple[Path, Path, Serve], flag: bool
) -> None:
    _, settings, serve = hook_profile
    report = run(hook_profile, authorize=flag, answers=[] if flag else [True])
    assert report.exit_code == 0 and "installed" in "\n".join(report.lines)
    assert default_hook_path().is_file() and "statusLine" in json.loads(settings.read_text())
    assert json.loads(settings.read_text())["unrelated"] == {"keep": "private-marker"}
    assert serve.mutations


def test_creation_refusal_after_install_authorization_blocks_serve(
    hook_profile: tuple[Path, Path, Serve],
) -> None:
    _, settings, serve = hook_profile
    settings.unlink()
    report = run(hook_profile, authorize=True, answers=[False])
    assert report.exit_code != 0 and serve.mutations == [] and not settings.exists()
    assert "creation" in "\n".join(report.lines).lower()
    assert not default_hook_path().exists()


def test_separate_creation_consent_can_succeed(hook_profile: tuple[Path, Path, Serve]) -> None:
    _, settings, serve = hook_profile
    settings.unlink()
    assert run(hook_profile, authorize=True, answers=[True]).exit_code == 0
    assert "statusLine" in json.loads(settings.read_text()) and serve.mutations


def test_custom_statusline_conflict_stops_without_replacement_or_serve(
    hook_profile: tuple[Path, Path, Serve],
) -> None:
    _, settings, serve = hook_profile
    settings.write_text('{"statusLine":{"type":"command","command":"grant=private-marker"}}')
    before = settings.read_bytes()
    report = run(hook_profile, authorize=True)
    assert report.exit_code != 0 and serve.mutations == [] and settings.read_bytes() == before
    text = "\n".join(report.lines)
    assert "conflict" in text and "private-marker" not in text


@pytest.mark.parametrize(
    "state", ["unreadable", "concurrent_edit", "resource_invalid", "private-marker"]
)
def test_other_authorized_hook_failures_stop_with_content_free_guidance(
    hook_profile: tuple[Path, Path, Serve], monkeypatch: pytest.MonkeyPatch, state: str
) -> None:
    from relaydeck.usage.hook_install import HookResult

    monkeypatch.setattr(setup, "install_hook", lambda *args, **kwargs: HookResult(state))
    result = run(hook_profile, authorize=True)
    assert result.exit_code != 0 and hook_profile[2].mutations == []
    assert ("Reinstall" if state == "resource_invalid" else "usage doctor") in "\n".join(
        result.lines
    )
    assert "private-marker" not in "\n".join(result.lines)


def test_failed_rollback_stops_setup_with_fixed_recovery_guidance(
    hook_profile: tuple[Path, Path, Serve], monkeypatch: pytest.MonkeyPatch
) -> None:
    _, settings, serve = hook_profile
    real = hook_files.atomic
    finalization_failed = False

    def fail(path: Path, fd: int, raw: bytes, *args: Any, **kwargs: Any) -> None:
        nonlocal finalization_failed
        if (
            path.parent == default_hook_path().parent
            and path.suffix == ".json"
            and json.loads(raw)["state"] == "installed"
        ):
            finalization_failed = True
            raise OSError("cookie=private-marker")
        if finalization_failed and path == settings:
            raise OSError("grant=secret")
        real(path, fd, raw, *args, **kwargs)

    monkeypatch.setattr(hook_files, "atomic", fail)
    report = run(hook_profile, authorize=True)
    assert report.exit_code != 0 and serve.mutations == []
    text = "\n".join(report.lines)
    assert "recovery_required" in text and "receipt" in text and "uninstall" in text
    assert "private-marker" not in text and "secret" not in text
    assert "statusLine" in json.loads(settings.read_text())  # Real partial commit retained.


def test_offline_usage_alias_and_top_level_forwarding_never_load_full_config_or_preflight(
    hook_profile: tuple[Path, Path, Serve],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    profile, _, serve = hook_profile

    def forbidden(*args: Any, **kwargs: Any) -> None:
        raise AssertionError("unexpected config/prerequisite/secret probe")

    monkeypatch.setattr(companion_cli, "load_config", forbidden)
    monkeypatch.setattr(companion_cli, "preflight", forbidden)
    assert companion_cli.main(["--config", str(profile), "usage", "doctor"]) == 0
    assert cli.main(["--config", str(profile), "usage", "doctor"]) == 0
    assert serve.calls == [] and "private-marker" not in capsys.readouterr().out


def test_setup_cli_forwards_install_flag_independently_of_yes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: list[dict[str, Any]] = []

    def capture(**kwargs: Any) -> setup.SetupReport:
        seen.append(kwargs)
        return setup.SetupReport((), 0)

    monkeypatch.setattr(cli, "run_setup", capture)
    # Plain setup needs a terminal to answer prompts; simulate one here.
    monkeypatch.setattr("sys.stdin.isatty", lambda: True)
    assert cli.main(["setup", "--yes", "--no-probe"]) == 0
    assert cli.main(["setup", "--install-limits-hook", "--no-probe"]) == 0
    assert (seen[0]["assume_yes"], seen[0]["install_limits_hook"]) == (True, False)
    assert (seen[1]["assume_yes"], seen[1]["install_limits_hook"]) == (False, True)
