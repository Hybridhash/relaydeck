from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import subprocess
import sys
import tomllib
from importlib.resources import files
from pathlib import Path

import pytest

from relaydeck.companion.config import UsageConfig, UsageSourceConfig
from relaydeck.gateway.usage import UsageService
from relaydeck.usage import hook_cli
from relaydeck.usage.contracts import LIMIT_STALE_MS
from relaydeck.usage.hook_doctor import cache_state, doctor_hook
from relaydeck.usage.hook_install import install_hook
from relaydeck.usage.hook_resource import hook_bytes
from relaydeck.usage.hook_setup import (
    HOOK_FILENAME,
    HOOK_SHA256,
    PREVIOUS_HOOK_SHA256,
    default_hook_path,
)

NOW = 2_000_000


def source(root: Path) -> UsageSourceConfig:
    return UsageSourceConfig(
        "kind-a",
        "jsonl-message-usage-v1",
        root,
        "Kind A",
        limits_format="json-statusline-limits-v1",
        limits_cache=root / "cache.json",
        limits_settings=root / "settings.json",
    )


def write(path: Path, body: object) -> None:
    path.write_text(json.dumps(body))
    path.chmod(0o600)


@pytest.mark.parametrize("state", ["missing", "fresh", "stale", "expired", "invalid"])
def test_cache_state_is_independent_and_doctor_is_read_only(tmp_path: Path, state: str) -> None:
    root = tmp_path.resolve()
    cache, settings, hooks = root / "cache.json", root / "settings.json", root / "hooks"
    write(settings, {})
    assert install_hook(settings, cache, confirmed=True, hooks_dir=hooks).state == "installed"
    if state != "missing":
        write(
            cache,
            {
                "captured_ms": NOW - (LIMIT_STALE_MS + 1 if state == "stale" else 1),
                "windows": {
                    "five_hour": {
                        "used_percentage": 22,
                        "resets_at": (NOW // 1000 - 1 if state == "expired" else 99999),
                    }
                },
            },
        )
        if state == "invalid":
            cache.write_text('{"private-marker":"cookie=secret"}')
    before = {p: (p.read_bytes(), p.stat().st_mtime_ns) for p in root.rglob("*") if p.is_file()}
    result = doctor_hook(settings, cache, hooks_dir=hooks, now_ms=NOW)
    assert result == {
        "installation": {"state": "installed", "checked_ms": NOW},
        "cache": {"state": state, "checked_ms": NOW},
    }
    assert "secret" not in json.dumps(result) and str(root) not in json.dumps(result)
    assert before == {
        p: (p.read_bytes(), p.stat().st_mtime_ns) for p in root.rglob("*") if p.is_file()
    }
    # Doctor checks immediately, without the gateway's minute-long throttle.
    settings.write_text('{"statusLine":{"type":"command","command":"do not run"}}')
    assert (
        doctor_hook(settings, cache, hooks_dir=hooks, now_ms=NOW + 1)["installation"]["state"]
        == "conflict"
    )


@pytest.mark.parametrize(
    "invalid",
    [
        "future",
        "duplicate",
        "bool",
        "nan",
        "extra",
        "unsafe",
        "symlink",
        "parent_symlink",
        "too_large",
        "nonobject",
    ],
)
def test_cache_validation_never_leaks_or_executes(tmp_path: Path, invalid: str) -> None:
    root = tmp_path.resolve()
    cache = root / "cache"
    body = {
        "captured_ms": NOW,
        "windows": {"five_hour": {"used_percentage": 4, "resets_at": 99999}},
    }
    write(cache, body)
    if invalid == "future":
        write(cache, {**body, "captured_ms": NOW + 1})
    elif invalid == "duplicate":
        cache.write_text('{"captured_ms":1,"captured_ms":2,"windows":{}}')
    elif invalid == "bool":
        body["windows"]["five_hour"]["used_percentage"] = True
        write(cache, body)
    elif invalid == "nan":
        cache.write_text('{"private-marker":NaN}')
    elif invalid == "extra":
        write(cache, {**body, "secret": "do not report"})
    elif invalid == "unsafe":
        cache.chmod(0o644)
    elif invalid == "symlink":
        cache.unlink()
        cache.symlink_to(root / "absent")
    elif invalid == "parent_symlink":
        alias = root / "alias"
        alias.symlink_to(root, target_is_directory=True)
        cache = alias / "cache"
    elif invalid == "too_large":
        cache.write_bytes(b"x" * (64 * 1024 + 1))
    elif invalid == "nonobject":
        cache.write_text("[]")
    assert cache_state(cache, NOW) == "invalid"


def test_unmanaged_installation_is_not_broken_collection(tmp_path: Path) -> None:
    assert doctor_hook(None, tmp_path.resolve() / "cache", now_ms=NOW) == {
        "installation": None,
        "cache": {"state": "missing", "checked_ms": NOW},
    }


def test_symlink_interpreter_recognized_without_resolving(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path.resolve()
    python = root / "tool python"
    python.symlink_to(sys.executable)
    monkeypatch.setattr(sys, "executable", str(python))
    settings, cache, hooks = root / "settings", root / "cache", root / "hooks"
    write(settings, {})
    assert install_hook(settings, cache, confirmed=True, hooks_dir=hooks).state == "installed"
    assert str(python) in json.loads(settings.read_text())["statusLine"]["command"]
    assert (
        doctor_hook(settings, cache, hooks_dir=hooks, now_ms=NOW)["installation"]["state"]
        == "installed"
    )


@pytest.mark.parametrize("correction", ["extra_line_field", "invalid_json_constant"])
def test_installation_requires_exact_managed_json(tmp_path: Path, correction: str) -> None:
    root = tmp_path.resolve()
    settings, cache, hooks = root / "settings", root / "cache", root / "hooks"
    write(settings, {})
    assert install_hook(settings, cache, confirmed=True, hooks_dir=hooks).state == "installed"
    body = json.loads(settings.read_text())
    if correction == "extra_line_field":
        body["statusLine"]["extra"] = "do not execute"
        write(settings, body)
    else:
        settings.write_text(json.dumps(body)[:-1] + ',"unexpected":NaN}')
    assert doctor_hook(settings, cache, hooks_dir=hooks, now_ms=NOW)["installation"]["state"] == (
        "conflict" if correction == "extra_line_field" else "invalid"
    )


def test_wheel_resource_config_hash_and_development_shim() -> None:
    package = files("relaydeck.usage.resources")
    canonical = package.joinpath(HOOK_FILENAME).read_bytes()
    assert hook_bytes() == canonical
    assert hashlib.sha256(canonical).hexdigest() == HOOK_SHA256
    manifest = json.loads(package.joinpath("hook_manifest.json").read_bytes())
    assert manifest == {
        "version": 1,
        "script": HOOK_FILENAME,
        "sha256": HOOK_SHA256,
        "previous_sha256": list(PREVIOUS_HOOK_SHA256),
    }
    repo = Path(__file__).parents[2]
    config = tomllib.loads((repo / "pyproject.toml").read_text())
    assert "src/relaydeck" in config["tool"]["hatch"]["build"]["targets"]["wheel"]["packages"]
    included = config["tool"]["hatch"]["build"]["targets"]["wheel"]["force-include"]
    for filename in (HOOK_FILENAME, "hook_manifest.json"):
        assert (
            included[f"src/relaydeck/usage/resources/{filename}"]
            == f"relaydeck/usage/resources/{filename}"
        )
    shim = (repo / "scripts/statusline_limits_hook.py").read_text()
    assert "from relaydeck.usage.resources.statusline_limits_hook import" in shim
    assert "def persist" not in shim and "def main" not in shim


def test_installed_hook_standalone_without_repo_or_environment(tmp_path: Path) -> None:
    root = tmp_path.resolve()
    script = root / HOOK_FILENAME
    script.write_bytes(hook_bytes())
    script.chmod(0o600)
    cache = root / "state/cache.json"
    result = subprocess.run(
        [sys.executable, "-I", "-B", str(script), "--cache-path", str(cache)],
        input=json.dumps(
            {
                "private": "ticket=secret",
                "rate_limits": {"five_hour": {"used_percentage": 25, "resets_at": 4102444800}},
            }
        ),
        text=True,
        capture_output=True,
        timeout=5,
        cwd=root,
        env={},
    )
    assert result.returncode == 0 and not result.stderr
    assert "secret" not in result.stdout
    body = json.loads(cache.read_text())
    assert set(body) == {"captured_ms", "windows"}
    assert body["windows"] == {"five_hour": {"used_percentage": 25, "resets_at": 4102444800}}
    assert cache.stat().st_mode & 0o777 == 0o600


def test_gateway_checks_new_install_location_not_old_state_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path.resolve()
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: root))
    configured = source(root)
    assert configured.limits_settings and configured.limits_cache
    write(configured.limits_settings, {})
    assert default_hook_path() == root / ".config/relaydeck/hooks" / HOOK_FILENAME
    assert (
        install_hook(configured.limits_settings, configured.limits_cache, confirmed=True).state
        == "installed"
    )
    service = UsageService(
        None, UsageConfig("UTC", (configured,)), "home-mac", root / "state", lambda: NOW
    )
    try:
        check = service._checks[(configured.kind, configured.format)]
        assert check.script == default_hook_path()
        assert check.check(NOW)["state"] == "installed"
    finally:
        asyncio.run(service.close())


def cli_args(*args: str) -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    hook_cli.add_usage_parser(parser.add_subparsers(dest="command", required=True))
    return parser.parse_args(["usage", *args])


def test_cli_separate_consent_and_content_free_doctor(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    root = tmp_path.resolve()
    configured, hooks = source(root), root / "hooks"
    assert configured.limits_settings
    write(configured.limits_settings, {"private": "grant=secret"})
    assert (
        hook_cli.run_usage(
            cli_args("install", "--source", "kind-a/jsonl-message-usage-v1"),
            [configured],
            hooks_dir=hooks,
        )
        == 1
    )
    assert not hooks.exists()
    assert (
        hook_cli.run_usage(
            cli_args(
                "install", "--source", "kind-a/jsonl-message-usage-v1", "--install-limits-hook"
            ),
            [configured],
            hooks_dir=hooks,
        )
        == 0
    )
    assert hook_cli.run_usage(cli_args("doctor"), [configured], hooks_dir=hooks) == 0
    assert (
        hook_cli.run_usage(
            cli_args("uninstall", "--source", "kind-a/jsonl-message-usage-v1"),
            [configured],
            hooks_dir=hooks,
        )
        == 0
    )
    output = capsys.readouterr().out
    assert '"installed"' in output and '"missing"' in output
    assert "secret" not in output and str(root) not in output and "command" not in output


def test_cli_missing_settings_requires_second_flag(tmp_path: Path) -> None:
    configured, hooks = source(tmp_path.resolve()), tmp_path.resolve() / "hooks"
    args = ["install", "--source", "kind-a/jsonl-message-usage-v1", "--install-limits-hook"]
    assert hook_cli.run_usage(cli_args(*args), [configured], hooks_dir=hooks) == 1
    assert not hooks.exists()
    assert (
        hook_cli.run_usage(cli_args(*args, "--create-settings"), [configured], hooks_dir=hooks) == 0
    )


def test_standalone_cli_never_runs_prerequisites_or_reads_secrets(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    root = tmp_path.resolve()
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: root))
    config = root / "private.toml"
    config.write_text(
        'tailscale_auto=true\nagent_kinds=["kind-a"]\n[usage]\ntimezone="UTC"\n[[usage.sources]]\nkind="kind-a"\nformat="jsonl-message-usage-v1"\nroot="/missing-transcript-root"\nlimits_format="json-statusline-limits-v1"\n'
        + f"limits_settings={json.dumps(str(root / 'settings'))}\n"
        + f"limits_cache={json.dumps(str(root / 'cache'))}\n"
    )
    config.chmod(0o600)

    def forbidden(*args: object, **kwargs: object) -> None:
        raise AssertionError("unexpected infrastructure/secret access")

    monkeypatch.setattr("relaydeck.companion.setup.preflight", forbidden)
    monkeypatch.setattr("relaydeck.companion.config.CompanionConfig.secrets", forbidden)
    monkeypatch.setattr("relaydeck.companion.config.load_config", forbidden)
    monkeypatch.setattr("relaydeck.companion.tailscale.private_origin", forbidden)
    assert hook_cli.main(["--config", str(config), "doctor"]) == 0
    assert "private.toml" not in capsys.readouterr().out


def test_cli_config_error_is_fixed_without_exception_or_cause(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def forbidden(path: Path) -> None:
        raise ValueError("grant=private-marker") from OSError("cookie=secret")

    monkeypatch.setattr(hook_cli, "load_hook_sources", forbidden)
    assert hook_cli.main(["--config", str(tmp_path / "private.toml"), "doctor"]) == 2
    assert capsys.readouterr().out.strip() == '{"state": "config_unavailable"}'


@pytest.mark.parametrize("unsafe", ["symlink", "nonprivate", "duplicate", "shared_cache"])
def test_offline_cli_rejects_invalid_config_without_mutation(
    tmp_path: Path, unsafe: str, capsys: pytest.CaptureFixture[str]
) -> None:
    root = tmp_path.resolve()
    config = root / "config.toml"
    if unsafe in ("symlink", "nonprivate"):
        config.write_text("agent_kinds=[]\n")
        config.chmod(0o600)
        if unsafe == "symlink":
            alias = root / "alias.toml"
            alias.symlink_to(config)
            config = alias
        else:
            config.chmod(0o644)
    elif unsafe == "duplicate":
        config.write_text("agent_kinds=[]\nagent_kinds=[]\n")
        config.chmod(0o600)
    else:
        row = (
            '\n[[usage.sources]]\nkind="{kind}"\nformat="jsonl-message-usage-v1"\nlimits_format="json-statusline-limits-v1"\nlimits_cache='
            + json.dumps(str(root / "cache"))
            + "\n"
        )
        config.write_text(
            'agent_kinds=["kind-a","kind-b"]\n[usage]\n'
            + row.format(kind="kind-a")
            + row.format(kind="kind-b")
        )
        config.chmod(0o600)
    before = config.read_bytes()
    assert hook_cli.main(["--config", str(config), "doctor"]) == 2
    assert capsys.readouterr().out.strip() == '{"state": "config_unavailable"}'
    assert config.read_bytes() == before and not (root / "cache").exists()
