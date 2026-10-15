"""`relaydeck setup --check` is read-only; a usage source is not a start permission.

Every external read is a fake: the prerequisite report, the companion state, the web bundle and the
usage report. Each test uses a temporary directory standing in for the home directory, so the real
configuration and secrets are never touched. Secret values are asserted absent from the output.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from relaydeck import cli
from relaydeck.companion import prereqs, readiness
from relaydeck.companion.config import load_config
from tests.support.companion.setup_sources import CUM_CLI, MESSAGE_READER, MSG_CLI

ORIGIN = "https://mac.example-tailnet.ts.net"
COOKIE_SECRET = "COOKIE-SECRET-VALUE-" + "c" * 32
CURSOR_SECRET = "CURSOR-SECRET-VALUE-" + "d" * 32
CONNECTOR_TOKEN = "CONNECTOR-TOKEN-VALUE-" + "t" * 32


def _ok_report(*, serve: prereqs.Result | None = None) -> prereqs.Report:
    return prereqs.Report(
        results=(
            prereqs.Result("tailscale", prereqs.STATE_OK),
            serve or prereqs.Result("tailscale_serve", prereqs.STATE_OK, detail="port 8445"),
            prereqs.Result("herdr", prereqs.STATE_OK),
            prereqs.Result("uv", prereqs.STATE_OK),
            prereqs.Result("python", prereqs.STATE_OK),
        )
    )


def _missing_report() -> prereqs.Report:
    return prereqs.Report(
        results=(
            prereqs.Result("tailscale", prereqs.STATE_MISSING),
            prereqs.Result("tailscale_serve", prereqs.STATE_MISSING),
            prereqs.Result("herdr", prereqs.STATE_MISSING),
            prereqs.Result("uv", prereqs.STATE_MISSING),
            prereqs.Result("python", prereqs.STATE_MISSING),
        )
    )


def _write(path: Path, text: str, mode: int = 0o600) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    path.chmod(mode)


def _config_text(
    home: Path, *, origin: str = ORIGIN, web_ui: bool = True, kinds: str = "[]"
) -> str:
    return "\n".join(
        [
            f'advertised_origin = "{origin}"',
            'host_id = "home-mac"',
            f"web_ui = {'true' if web_ui else 'false'}",
            f'state_dir = "{home / "state"}"',
            f'cookie_secret_file = "{home / "cookie.secret"}"',
            f'cursor_secret_file = "{home / "cursor.secret"}"',
            f'connector_credentials = "{home / "connector.json"}"',
            f'herdr_socket = "{home / "herdr.sock"}"',
            f"agent_kinds = {kinds}",
            "",
        ]
    )


def _ready(home: Path, *, web_ui: bool = True) -> Path:
    """A fully ready fake configuration under `home`. Returns the config path."""
    (home / "state").mkdir(mode=0o700, parents=True, exist_ok=True)
    (home / "state").chmod(0o700)
    config = home / "companion.toml"
    _write(config, _config_text(home, web_ui=web_ui))
    _write(home / "cookie.secret", COOKIE_SECRET)
    _write(home / "cursor.secret", CURSOR_SECRET)
    _write(
        home / "connector.json",
        json.dumps({"gateway_url": ORIGIN, "host_id": "home-mac", "host_token": CONNECTOR_TOKEN}),
    )
    return config


def _run(config: Path, **overrides: object) -> readiness.Readiness:
    options: dict[str, object] = {
        "prereq_report": _ok_report(),
        "companion_state": lambda _state_dir: "running",
        "web_current": lambda: Path("/fake/bundle"),
        "usage": None,
    }
    options.update(overrides)
    return readiness.run_check(config, **options)  # type: ignore[arg-type]


def _item(report: readiness.Readiness, number: int) -> readiness.Item:
    return next(item for item in report.items if item.number == number)


def _lines(report: readiness.Readiness) -> str:
    return "\n".join(cli.readiness_lines(report, "/no/such/home"))


def _snapshot(root: Path) -> set[str]:
    return {str(path) for path in root.rglob("*")}


# -- the ten items, each mapped to a test ----------------------------------------------------------


def test_an_empty_home_is_everything_not_ready_with_next_steps_and_creates_no_files(
    tmp_path: Path,
) -> None:
    before = _snapshot(tmp_path)
    report = readiness.run_check(
        tmp_path / "companion.toml",
        prereq_report=_missing_report(),
        companion_state=lambda _state_dir: "stopped",
        web_current=lambda: None,
        usage=None,
    )
    assert report.exit_code == 1
    for number in (1, 2, 3, 4, 5, 6, 7, 11):
        assert _item(report, number).state == readiness.FAIL, number
        assert _item(report, number).next_step, number
    assert _snapshot(tmp_path) == before


def test_a_fully_ready_fake_passes_every_item_and_is_ready_to_pair(tmp_path: Path) -> None:
    report = _run(_ready(tmp_path))
    assert report.exit_code == 0
    assert report.ready_to_pair is True
    assert all(item.state == readiness.OK for item in report.items)
    assert _item(report, 11).reason.startswith("ready to pair")


def test_a_placeholder_configuration_is_refused(tmp_path: Path) -> None:
    config = tmp_path / "companion.toml"
    _write(config, _config_text(tmp_path, origin="https://your-mac.your-tailnet.ts.net"))
    item = _item(_run(config), 2)
    assert item.state == readiness.FAIL
    assert "template origin" in item.reason


def test_group_readable_secrets_are_refused_and_never_printed(tmp_path: Path) -> None:
    config = _ready(tmp_path)
    (tmp_path / "cookie.secret").chmod(0o640)
    report = _run(config)
    item = _item(report, 3)
    assert item.state == readiness.FAIL
    assert "not owner-only" in item.reason
    assert report.ready_to_pair is False
    assert COOKIE_SECRET not in _lines(report)


def test_missing_web_assets_are_reported_when_the_web_app_is_on(tmp_path: Path) -> None:
    report = _run(_ready(tmp_path), web_current=lambda: None)
    assert _item(report, 5).state == readiness.FAIL
    assert "relaydeck web install" in _item(report, 5).next_step


def test_the_web_item_is_not_needed_for_a_bridge_only_companion(tmp_path: Path) -> None:
    report = _run(_ready(tmp_path, web_ui=False), web_current=lambda: None)
    assert _item(report, 5).state == readiness.OK


def test_a_companion_that_is_not_running_blocks_pairing(tmp_path: Path) -> None:
    report = _run(_ready(tmp_path), companion_state=lambda _state_dir: "stopped")
    assert _item(report, 6).state == readiness.FAIL
    assert _item(report, 6).next_step == "start it: relaydeck-companion start"
    assert _item(report, 11).state == readiness.FAIL
    assert report.ready_to_pair is False


def test_a_serve_port_mismatch_is_refused(tmp_path: Path) -> None:
    mismatch = prereqs.Result(
        "tailscale_serve", prereqs.STATE_NEEDS_ACTION, prereqs.GUIDANCE["serve_conflict"]
    )
    report = _run(_ready(tmp_path), prereq_report=_ok_report(serve=mismatch))
    assert _item(report, 7).state == readiness.FAIL
    assert report.ready_to_pair is False


def test_the_enrollment_must_match_this_host_and_origin(tmp_path: Path) -> None:
    config = _ready(tmp_path)
    _write(
        tmp_path / "connector.json",
        json.dumps(
            {
                "gateway_url": "https://other.example-tailnet.ts.net",
                "host_id": "home-mac",
                "host_token": CONNECTOR_TOKEN,
            }
        ),
    )
    item = _item(_run(config), 4)
    assert item.state == readiness.FAIL
    assert "another origin" in item.reason
    assert CONNECTOR_TOKEN not in _lines(_run(config))


def test_usage_sources_are_checked_for_their_root_and_are_optional(tmp_path: Path) -> None:
    config = tmp_path / "companion.toml"
    root = tmp_path / "usage-root"
    root.mkdir()
    text = _config_text(tmp_path, kinds=f'["{CUM_CLI.id}"]') + "\n".join(
        [
            "[usage]",
            'timezone = "Etc/UTC"',
            "[[usage.sources]]",
            f'kind = "{MSG_CLI.id}"',
            f'format = "{MESSAGE_READER}"',
            f'root = "{root}"',
            f'display_name = "{MSG_CLI.display_name}"',
        ]
    )
    _write(config, text)
    _write(tmp_path / "cookie.secret", COOKIE_SECRET)
    _write(tmp_path / "cursor.secret", CURSOR_SECRET)
    (tmp_path / "state").mkdir(mode=0o700)
    report = readiness.run_check(
        config,
        prereq_report=_ok_report(),
        companion_state=lambda _state_dir: "running",
        web_current=lambda: Path("/fake/bundle"),
        usage=None,
    )
    assert _item(report, 8).state == readiness.WARN  # the source exists but was not detected


# -- a usage source is not a start permission --------------------------------------


def test_a_usage_kind_outside_agent_kinds_is_accepted_and_agent_kinds_is_unchanged(
    tmp_path: Path,
) -> None:
    config = tmp_path / "companion.toml"
    root = tmp_path / "usage-root"
    root.mkdir()
    text = _config_text(tmp_path, kinds=f'["{CUM_CLI.id}"]') + "\n".join(
        [
            "[usage]",
            'timezone = "Etc/UTC"',
            "[[usage.sources]]",
            f'kind = "{MSG_CLI.id}"',
            f'format = "{MESSAGE_READER}"',
            f'root = "{root}"',
            f'display_name = "{MSG_CLI.display_name}"',
        ]
    )
    _write(config, text)
    loaded = load_config(config)
    assert loaded.agent_kinds == (CUM_CLI.id,)
    assert loaded.usage is not None and [s.kind for s in loaded.usage.sources] == [MSG_CLI.id]


def test_a_malformed_usage_kind_is_still_refused(tmp_path: Path) -> None:
    config = tmp_path / "companion.toml"
    root = tmp_path / "usage-root"
    root.mkdir()
    text = _config_text(tmp_path, kinds=f'["{CUM_CLI.id}"]') + "\n".join(
        [
            "[usage]",
            'timezone = "Etc/UTC"',
            "[[usage.sources]]",
            'kind = "Not Valid!"',
            f'format = "{MESSAGE_READER}"',
            f'root = "{root}"',
            'display_name = "Bad"',
        ]
    )
    _write(config, text)
    # Optional usage fails closed: the malformed source is dropped; the profile still loads.
    loaded = load_config(config)
    assert loaded.usage is None
    assert loaded.agent_kinds == (CUM_CLI.id,)


# -- non-terminal setup makes zero writes ----------------------------------------------------------


def test_plain_setup_without_a_terminal_writes_nothing_and_exits_nonzero(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr("sys.stdin.isatty", lambda: False)
    config = tmp_path / "companion.toml"
    before = _snapshot(tmp_path)
    code = cli.main(["--config", str(config), "setup"])
    assert code == 1
    assert _snapshot(tmp_path) == before
    out = capsys.readouterr().out
    assert "Nothing was written." in out
    assert "Pending decisions" in out


def test_the_check_flag_is_read_only_and_exits_with_the_report_code(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    before = _snapshot(tmp_path)
    monkeypatch.setattr(cli, "_check_report", lambda _config: _run(_ready(tmp_path)))
    code = cli.main(["--config", str(tmp_path / "companion.toml"), "setup", "--check"])
    assert code == 0
    assert _snapshot(tmp_path) - before <= {str(tmp_path / "companion.toml")} | {
        str(tmp_path / name) for name in ("cookie.secret", "cursor.secret", "connector.json")
    } | {str(tmp_path / "state")}


# -- output never shows a secret, and home paths read as ~ ----------------------------------------


def test_secret_values_are_never_printed(tmp_path: Path) -> None:
    config = _ready(tmp_path)
    text = _lines(_run(config))
    for secret in (COOKIE_SECRET, CURSOR_SECRET, CONNECTOR_TOKEN):
        assert secret not in text


def test_home_paths_are_shown_as_tilde(tmp_path: Path) -> None:
    lines = cli.readiness_lines(_run(_ready(tmp_path)), str(tmp_path))
    assert all(str(tmp_path) not in line for line in lines)


def test_the_symbols_mark_each_item_state(tmp_path: Path) -> None:
    text = _lines(_run(_ready(tmp_path), companion_state=lambda _state_dir: "stopped"))
    assert "✓  1." in text or "✓ 1." in text
    assert "✗" in text
