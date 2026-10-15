import json
from importlib.resources import files

import pytest

from relaydeck.usage.hook_setup import HOOK_SHA256, HookSetupCheck, managed_command


@pytest.mark.parametrize(
    "state",
    [
        "not_configured",
        "script_missing",
        "script_outdated",
        "settings_missing",
        "statusline_missing",
        "conflict",
        "installed",
        "invalid",
        "unreadable",
    ],
)
def test_installation_states_are_positive_readonly_evidence(tmp_path, state, monkeypatch):
    root = tmp_path.resolve()
    script, settings, cache, interpreter = (
        root / x for x in ("hook.py", "settings", "cache", "python")
    )
    script.write_bytes(
        files("relaydeck.usage.resources").joinpath("statusline_limits_hook.py").read_bytes()
    )
    script.chmod(0o600)
    interpreter.write_text("not executed")
    interpreter.chmod(0o700)
    settings.write_text(
        json.dumps(
            {
                "statusLine": {
                    "type": "command",
                    "command": managed_command(script, cache, interpreter),
                },
                "unrelated": 3,
            }
        )
    )
    settings.chmod(0o600)
    check = HookSetupCheck(settings, cache, script, interpreter=interpreter)
    before = settings.read_bytes()
    if state == "not_configured":
        check.script = None
    elif state == "script_missing":
        script.unlink()
    elif state == "script_outdated":
        script.write_text("modified")
    elif state == "settings_missing":
        settings.unlink()
    elif state == "statusline_missing":
        settings.write_text('{"unrelated":3}')
    elif state == "conflict":
        settings.write_text('{"statusLine":{"type":"command","command":"never run"}}')
    elif state == "invalid":
        settings.write_text('{"statusLine":1,"statusLine":2}')
    elif state == "unreadable":

        def denied(*a, **k):
            raise PermissionError("secret")

        monkeypatch.setattr("relaydeck.usage.hook_setup._read", denied)
    assert check.check(1234) == {"state": state, "checked_ms": 1234}
    if state == "installed":
        assert settings.read_bytes() == before
    assert len(HOOK_SHA256) == 64


def test_setup_age_and_unmanaged_are_separate_from_limits_freshness(tmp_path):
    now = [0.0]
    check = HookSetupCheck(tmp_path.resolve() / "settings", None, None, monotonic=lambda: now[0])
    assert check.check(100) == {"state": "not_configured", "checked_ms": 100}
    now[0] = 59.9
    assert check.check(999) == {"state": "not_configured", "checked_ms": 100}
    now[0] = 60
    assert check.check(1000) == {"state": "not_configured", "checked_ms": 1000}
    assert HookSetupCheck(None, None, None).check(100) is None


def test_default_interpreter_keeps_the_absolute_tool_environment_symlink(tmp_path, monkeypatch):
    root = tmp_path.resolve()
    base = root / "base-python"
    base.write_text("never executed")
    base.chmod(0o700)
    interpreter = root / "tool-env" / "bin" / "python"
    interpreter.parent.mkdir(parents=True)
    interpreter.symlink_to(base)
    script, settings, cache = (root / name for name in ("hook.py", "settings", "cache"))
    script.write_bytes(
        files("relaydeck.usage.resources").joinpath("statusline_limits_hook.py").read_bytes()
    )
    script.chmod(0o600)
    settings.write_text(
        json.dumps(
            {
                "statusLine": {
                    "type": "command",
                    "command": managed_command(script, cache, interpreter),
                }
            }
        )
    )
    settings.chmod(0o600)
    monkeypatch.setattr("relaydeck.usage.hook_setup.sys.executable", str(interpreter))
    check = HookSetupCheck(settings, cache, script)
    assert check.check(123) == {"state": "installed", "checked_ms": 123}
    assert check.interpreter == interpreter
    # A resolved base-interpreter command is not the installed tool environment.
    settings.write_text(
        json.dumps(
            {"statusLine": {"type": "command", "command": managed_command(script, cache, base)}}
        )
    )
    assert HookSetupCheck(settings, cache, script).check(124)["state"] == "conflict"


@pytest.mark.parametrize("unsafe", ["symlink", "directory", "oversize", "mode", "nonobject"])
def test_setup_rejects_unsafe_or_unparseable_settings(tmp_path, unsafe):
    root = tmp_path.resolve()
    script = root / "hook.py"
    script.write_bytes(
        files("relaydeck.usage.resources").joinpath("statusline_limits_hook.py").read_bytes()
    )
    script.chmod(0o600)
    settings = root / "settings"
    settings.write_text("{}")
    settings.chmod(0o600)
    if unsafe == "symlink":
        settings.unlink()
        settings.symlink_to(script)
    elif unsafe == "directory":
        settings.unlink()
        settings.mkdir()
    elif unsafe == "oversize":
        settings.write_bytes(b"x" * (1024 * 1024 + 1))
    elif unsafe == "mode":
        settings.chmod(0o666)
    elif unsafe == "nonobject":
        settings.write_text("[]")
    assert HookSetupCheck(settings, root / "cache", script).check(123)["state"] == "invalid"
