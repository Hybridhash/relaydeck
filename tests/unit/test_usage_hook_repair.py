"""Detecting a replaced hook and repairing only RelayDeck's managed field (temp files only)."""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

from relaydeck.usage.hook_cli import run_usage
from relaydeck.usage.hook_doctor import doctor_sources
from relaydeck.usage.hook_install import install_hook, receipt_state
from relaydeck.usage.hook_repair import diagnose_source, repair_sources
from relaydeck.usage.hook_setup import HOOK_FILENAME, managed_command

PERMISSIONS = {"permissions": {"allow": ["Read"], "deny": ["Write"]}}


@dataclass
class Source:
    settings: Path
    cache: Path
    kind: str = "tool-a"
    format: str = "jsonl-message-usage-v1"
    limits_format: str | None = "json-statusline-limits-v1"

    @property
    def limits_settings(self) -> Path:
        return self.settings

    @property
    def limits_cache(self) -> Path:
        return self.cache


@dataclass
class Env:
    source: Source
    hooks: Path
    python: Path

    @property
    def managed(self) -> dict[str, str]:
        return {
            "type": "command",
            "command": managed_command(self.hooks / HOOK_FILENAME, self.source.cache, self.python),
        }

    def install(self, **kwargs: Any) -> str:
        return install_hook(
            self.source.settings,
            self.source.cache,
            confirmed=True,
            hooks_dir=self.hooks,
            interpreter=self.python,
            **kwargs,
        ).state

    def diagnose(self) -> Any:
        return diagnose_source(self.source, hooks_dir=self.hooks, interpreter=self.python)

    def repair(self, *, answer: bool = True, **kwargs: Any) -> list[Any]:
        asked: list[Any] = []

        def confirm(planned: Any) -> bool:
            asked.append(list(planned))
            return answer

        outcomes = repair_sources(
            [self.source],
            confirm=confirm,
            hooks_dir=self.hooks,
            interpreter=self.python,
            **kwargs,
        )
        self.asked = asked
        return outcomes

    def settings_json(self) -> dict[str, Any]:
        return json.loads(self.source.settings.read_text())

    def write(self, value: dict[str, Any]) -> None:
        self.source.settings.write_text(json.dumps(value))
        self.source.settings.chmod(0o600)

    def snapshot(self) -> dict[str, str]:
        root = self.source.settings.parent
        return {
            str(p.relative_to(root)): hashlib.sha256(p.read_bytes()).hexdigest()
            for p in sorted(root.rglob("*"))
            if p.is_file()
        }


@pytest.fixture
def env(tmp_path: Path) -> Env:
    root = tmp_path.resolve()
    python = root / "tool env/python"
    python.parent.mkdir(mode=0o700)
    python.symlink_to(Path(sys.executable))
    settings = root / "cli" / "settings.json"
    settings.parent.mkdir(mode=0o700)
    result = Env(Source(settings, root / "private/cache.json"), root / "hooks", python)
    result.write(PERMISSIONS)
    assert result.install() == "installed"
    return result


def test_a_healthy_hook_is_left_alone(env: Env) -> None:
    before = env.snapshot()
    assert env.diagnose().reason == "ok"
    (outcome,) = env.repair()
    assert outcome.action == "healthy"
    assert env.asked == [] and env.snapshot() == before


def test_settings_replaced_with_only_permissions_is_named_and_repaired(env: Env) -> None:
    env.write(PERMISSIONS)  # the real case: the file was overwritten with permission rules only
    diagnosis = env.diagnose()
    assert diagnosis.reason == "settings_replaced"
    assert diagnosis.repairable and "usage repair" in diagnosis.fix
    assert "tool-a/jsonl-message-usage-v1" in diagnosis.fix
    (outcome,) = env.repair()
    assert (outcome.action, outcome.result) == ("repaired", "installed")
    body = env.settings_json()
    assert body["statusLine"] == env.managed
    assert body["permissions"] == PERMISSIONS["permissions"]  # nothing else touched
    assert receipt_state(env.source.settings, hooks_dir=env.hooks) == "installed"
    assert env.diagnose().reason == "ok"


def test_a_repeated_repair_is_idempotent_and_writes_nothing(env: Env) -> None:
    env.write(PERMISSIONS)
    env.repair()
    after_first = env.snapshot()
    (outcome,) = env.repair()
    assert outcome.action == "healthy"
    assert env.snapshot() == after_first


def test_no_install_record_reads_as_managed_field_missing(tmp_path: Path) -> None:
    root = tmp_path.resolve()
    python = root / "py"
    python.symlink_to(Path(sys.executable))
    settings = root / "settings.json"
    settings.write_text(json.dumps(PERMISSIONS))
    settings.chmod(0o600)
    source = Source(settings, root / "cache.json")
    diagnosis = diagnose_source(source, hooks_dir=root / "hooks", interpreter=python)
    assert diagnosis.reason in ("managed_field_missing", "hook_script_missing")


def test_a_managed_field_with_another_interpreter_is_put_back_without_a_replace_flag(
    env: Env,
) -> None:
    command = env.managed["command"].replace(str(env.python), "/opt/other/python3.13")
    env.write({**PERMISSIONS, "statusLine": {"type": "command", "command": command}})
    assert env.diagnose().reason == "managed_field_edited"
    (outcome,) = env.repair()
    assert outcome.action == "repaired"
    body = env.settings_json()
    assert body["statusLine"] == env.managed and body["permissions"] == PERMISSIONS["permissions"]


@pytest.mark.parametrize(
    "suffix",
    [" | my-custom-status-renderer", " x", " && other-tool", "; echo done"],
)
def test_a_command_that_merely_contains_the_hook_is_custom_and_never_overwritten(
    env: Env, suffix: str
) -> None:
    wrapped = {"type": "command", "command": env.managed["command"] + suffix}
    env.write({**PERMISSIONS, "statusLine": wrapped})
    before = env.snapshot()
    assert env.diagnose().reason == "custom_statusline_present"
    (outcome,) = env.repair()
    assert outcome.action == "needs_consent" and "--replace-statusline" in outcome.fix
    assert env.asked == [] and env.snapshot() == before
    assert env.settings_json()["statusLine"] == wrapped


def test_a_wrapper_around_the_script_path_is_custom(env: Env) -> None:
    script = env.hooks / HOOK_FILENAME
    wrapped = {"type": "command", "command": f"/bin/sh -c 'python3 {script} --cache-path x'"}
    env.write({**PERMISSIONS, "statusLine": wrapped})
    assert env.diagnose().reason == "custom_statusline_present"


def test_a_change_during_confirmation_is_never_overwritten(env: Env) -> None:
    command = env.managed["command"].replace(str(env.python), "/opt/other/python3.13")
    env.write({**PERMISSIONS, "statusLine": {"type": "command", "command": command}})
    custom = {"type": "command", "command": "my-custom-status-renderer"}

    def confirm(planned: Any) -> bool:
        env.write({**PERMISSIONS, "statusLine": custom})  # the user edits while we ask
        return True

    outcomes = repair_sources(
        [env.source], confirm=confirm, hooks_dir=env.hooks, interpreter=env.python
    )
    (outcome,) = outcomes
    assert outcome.action == "changed_since_diagnosis"
    assert outcome.reason == "custom_statusline_present"
    assert "--replace-statusline" in outcome.fix
    assert env.settings_json()["statusLine"] == custom


def test_an_explicit_replace_flag_does_not_carry_over_to_a_value_changed_after_diagnosis(
    env: Env,
) -> None:
    env.write({**PERMISSIONS, "statusLine": {"type": "command", "command": "first-custom"}})
    second = {"type": "command", "command": "second-custom"}

    def confirm(planned: Any) -> bool:
        env.write({**PERMISSIONS, "statusLine": second})
        return True

    (outcome,) = repair_sources(
        [env.source],
        confirm=confirm,
        replace_statusline=True,
        hooks_dir=env.hooks,
        interpreter=env.python,
    )
    assert outcome.action == "changed_since_diagnosis"
    assert env.settings_json()["statusLine"] == second


def test_a_field_added_while_missing_is_not_replaced(env: Env) -> None:
    env.write(PERMISSIONS)
    custom = {"type": "command", "command": "added-meanwhile"}

    def confirm(planned: Any) -> bool:
        env.write({**PERMISSIONS, "statusLine": custom})
        return True

    (outcome,) = repair_sources(
        [env.source], confirm=confirm, hooks_dir=env.hooks, interpreter=env.python
    )
    assert outcome.action == "changed_since_diagnosis"
    assert env.settings_json()["statusLine"] == custom


def test_setup_does_not_replace_a_field_that_changed_while_it_asked(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from relaydeck.companion.setup import _limits_hook_setup

    home = tmp_path.resolve() / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    settings = home / "cli" / "settings.json"
    settings.parent.mkdir(mode=0o700)
    source = Source(settings, home / "private/cache.json")
    settings.write_text(json.dumps(PERMISSIONS))
    settings.chmod(0o600)
    assert install_hook(settings, source.cache, confirmed=True).state == "installed"
    script = home / ".config/relaydeck/hooks" / HOOK_FILENAME
    edited = managed_command(script, source.cache, Path("/opt/other/python3.13"))
    settings.write_text(
        json.dumps({**PERMISSIONS, "statusLine": {"type": "command", "command": edited}})
    )
    custom = {"type": "command", "command": "my-custom-status-renderer"}

    def confirm(_prompt: str) -> bool:
        settings.write_text(json.dumps({**PERMISSIONS, "statusLine": custom}))
        return True

    report = _limits_hook_setup([source], explicit=False, confirm=confirm)
    assert report.exit_code == 1
    assert any("changed while setup was asking" in line for line in report.lines)
    assert json.loads(settings.read_text())["statusLine"] == custom


def test_setup_leaves_a_wrapped_hook_command_alone(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from relaydeck.companion.setup import _limits_hook_setup

    home = tmp_path.resolve() / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    settings = home / "cli" / "settings.json"
    settings.parent.mkdir(mode=0o700)
    source = Source(settings, home / "private/cache.json")
    settings.write_text(json.dumps(PERMISSIONS))
    settings.chmod(0o600)
    assert install_hook(settings, source.cache, confirmed=True).state == "installed"
    script = home / ".config/relaydeck/hooks" / HOOK_FILENAME
    wrapped = {
        "type": "command",
        "command": managed_command(script, source.cache, Path(sys.executable)) + " | renderer",
    }
    settings.write_text(json.dumps({**PERMISSIONS, "statusLine": wrapped}))
    report = _limits_hook_setup([source], explicit=False, confirm=lambda _prompt: True)
    assert report.exit_code == 1
    assert json.loads(settings.read_text())["statusLine"] == wrapped


def test_a_custom_status_line_is_reported_and_never_overwritten(env: Env) -> None:
    custom = {**PERMISSIONS, "statusLine": {"type": "command", "command": "my-own-line"}}
    env.write(custom)
    before = env.snapshot()
    diagnosis = env.diagnose()
    assert diagnosis.reason == "custom_statusline_present"
    assert "--replace-statusline" in diagnosis.fix
    (outcome,) = env.repair()
    assert outcome.action == "needs_consent" and "--replace-statusline" in outcome.fix
    assert env.snapshot() == before and env.asked == []
    (replaced,) = env.repair(replace_statusline=True)
    assert replaced.action == "repaired"
    assert env.settings_json()["statusLine"] == env.managed


def test_missing_settings_need_their_own_consent(env: Env) -> None:
    env.source.settings.unlink()
    diagnosis = env.diagnose()
    assert diagnosis.reason == "settings_missing" and "--create-settings" in diagnosis.fix
    (outcome,) = env.repair()
    assert outcome.action == "needs_consent" and not env.source.settings.exists()
    (created,) = env.repair(create_settings=True)
    assert created.action == "repaired"
    assert env.settings_json() == {"statusLine": env.managed}


def test_declining_the_confirmation_changes_nothing(env: Env) -> None:
    env.write(PERMISSIONS)
    before = env.snapshot()
    (outcome,) = env.repair(answer=False)
    assert outcome.action == "confirmation_declined"
    assert len(env.asked) == 1 and env.snapshot() == before


def test_the_confirmation_lists_what_will_change(env: Env) -> None:
    env.write(PERMISSIONS)
    env.repair()
    (planned,) = env.asked[0]
    assert planned.change.startswith("add RelayDeck's statusLine field to ")
    assert "every other field is kept" in planned.change


def test_doctor_names_the_reason_and_writes_nothing(env: Env) -> None:
    env.write(PERMISSIONS)
    before = env.snapshot()
    (row,) = doctor_sources([env.source], hooks_dir=env.hooks)
    assert row["installation"]["state"] == "statusline_missing"
    assert row["diagnosis"]["reason"] == "settings_replaced"
    assert row["diagnosis"]["repairable"] is True
    assert row["diagnosis"]["fix"].startswith("relaydeck usage repair --source ")
    assert env.snapshot() == before


def _args(**values: Any) -> argparse.Namespace:
    base = {
        "usage_action": "repair",
        "source": None,
        "yes": False,
        "create_settings": False,
        "replace_statusline": False,
    }
    return argparse.Namespace(**{**base, **values})


def _process_env(tmp_path: Path) -> Env:
    """An environment whose hook uses the process interpreter, as the CLI does."""
    root = tmp_path.resolve() / "cli-env"
    settings = root / "cli" / "settings.json"
    settings.parent.mkdir(parents=True, mode=0o700)
    result = Env(
        Source(settings, root / "private/cache.json"), root / "hooks", Path(sys.executable)
    )
    result.write(PERMISSIONS)
    assert result.install() == "installed"
    result.write(PERMISSIONS)
    return result


def test_the_cli_repairs_with_yes_and_prints_json(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    env = _process_env(tmp_path)
    code = run_usage(_args(yes=True), [env.source], hooks_dir=env.hooks)
    out = json.loads(capsys.readouterr().out)
    assert code == 0 and out["repairs"][0]["action"] == "repaired"
    body = env.settings_json()
    assert body["permissions"] == PERMISSIONS["permissions"] and "statusLine" in body


def test_the_cli_without_yes_and_without_a_terminal_only_reports(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    env = _process_env(tmp_path)
    before = env.snapshot()
    monkeypatch.setattr(sys.stdin, "isatty", lambda: False)
    code = run_usage(_args(), [env.source], hooks_dir=env.hooks)
    out = json.loads(capsys.readouterr().out)
    assert code == 1 and out["repairs"][0]["action"] == "confirmation_declined"
    assert env.snapshot() == before


def test_setup_check_lists_the_problem_with_the_fix_command(
    env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    from types import SimpleNamespace

    from relaydeck.companion import readiness

    monkeypatch.setenv("HOME", str(env.hooks.parent / "home"))
    config = SimpleNamespace(usage=SimpleNamespace(sources=[env.source], limits=[]))
    item = readiness._limits_item(config)  # type: ignore[arg-type]
    assert item.state == readiness.WARN
    assert "relaydeck usage repair --source tool-a/jsonl-message-usage-v1" in item.next_step


def test_the_usage_card_says_why_and_where_to_repair() -> None:
    from relaydeck.client.features.usage import selectors as s
    from relaydeck.client.features.usage.screen_model import _setup_text

    view = s.ProviderLimits(
        kind="tool-a",
        name="Tool A",
        status=s.STATUS_SETUP_NEEDED,
        observed_ms=None,
        windows=(),
        setup_state="statusline_missing",
    )
    text = _setup_text(view, "Setup needed")
    assert "status line no longer runs RelayDeck's hook" in text
    assert "relaydeck usage repair" in text
    assert _setup_text(None, "Setup needed") == "Setup needed"
