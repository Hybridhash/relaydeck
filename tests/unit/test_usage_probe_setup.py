"""`usage install/doctor/repair` for a tool that reads its usage in RelayDeck's empty probe folder,
and the Usage view's hint when that folder isn't trusted yet."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from relaydeck.client.features.usage import model as usage_model
from relaydeck.client.features.usage import screen_model
from relaydeck.usage import hook_cli
from relaydeck.usage.probe_setup import (
    NOT_INSTALLED,
    NOT_TRUSTED,
    TRUSTED,
    ProbeTarget,
    doctor_line,
    load_probe_targets,
    offer_trust,
    probe_state,
)

THERE = "/usr/bin/muse"


def _trust(path: Path, *folders: Path) -> str:
    path.write_text(
        json.dumps(
            {"schema_version": 1, "projects": {str(f): {"decision": "trusted"} for f in folders}}
        )
    )
    return str(path)


def _target(tmp_path: Path, *, trusted: bool = False) -> ProbeTarget:
    folder = tmp_path / "state" / "muse-probe"
    store = _trust(tmp_path / "trust.json", *([folder] if trusted else []))
    if trusted:
        folder.mkdir(parents=True)
    return ProbeTarget("muse", "muse", folder, store)


def _which(found: bool) -> Any:
    return lambda _name: THERE if found else None


class Spawner:
    def __init__(self, trust_after: Path | None = None) -> None:
        self.calls: list[tuple[tuple[str, ...], Path]] = []
        self.trust_after = trust_after

    def __call__(self, argv: Any, cwd: Path) -> int:
        self.calls.append((tuple(argv), cwd))
        if self.trust_after is not None:
            _trust(self.trust_after, cwd)  # stands in for the user choosing Trust in the tool
        return 0


def test_install_creates_the_folder_private_and_empty(tmp_path: Path) -> None:
    target = _target(tmp_path)
    said: list[str] = []
    state = offer_trust(
        target, ask=lambda _p: False, interactive=True, which=_which(True), say=said.append
    )
    assert state == NOT_TRUSTED
    assert (target.folder.stat().st_mode & 0o777) == 0o700 and list(target.folder.iterdir()) == []
    assert any("asks once whether to trust it" in line for line in said)


def test_without_a_terminal_only_the_instruction_is_printed(tmp_path: Path) -> None:
    target = _target(tmp_path)
    spawn, said = Spawner(), []

    def never(_prompt: str) -> bool:
        raise AssertionError("must not ask without a terminal")

    state = offer_trust(
        target, ask=never, spawn=spawn, interactive=False, which=_which(True), say=said.append
    )
    assert state == NOT_TRUSTED and spawn.calls == []
    assert any(str(target.folder) in line and "choose Trust" in line for line in said)


def test_yes_spawns_exactly_the_tool_in_the_probe_folder_and_rereads_the_store(
    tmp_path: Path,
) -> None:
    target = _target(tmp_path)
    prompts: list[str] = []
    spawn = Spawner(trust_after=Path(str(target.trust_store)))
    said: list[str] = []

    def yes(prompt: str) -> bool:
        prompts.append(prompt)
        return True

    state = offer_trust(
        target, ask=yes, spawn=spawn, interactive=True, which=_which(True), say=said.append
    )
    assert prompts == ["Open muse there now so you can choose Trust? [y/N] "]
    assert spawn.calls == [(("muse",), target.folder)]
    assert state == TRUSTED and said[-1].endswith("is now trusted.")


def test_no_means_nothing_is_spawned_and_the_store_is_untouched(tmp_path: Path) -> None:
    target = _target(tmp_path)
    store = Path(str(target.trust_store))
    before = store.read_bytes(), store.stat().st_mtime_ns
    spawn = Spawner()
    offer_trust(target, ask=lambda _p: False, spawn=spawn, interactive=True, which=_which(True))
    assert spawn.calls == []
    assert (store.read_bytes(), store.stat().st_mtime_ns) == before


def test_a_trusted_folder_asks_nothing_and_a_missing_tool_creates_nothing(tmp_path: Path) -> None:
    trusted = _target(tmp_path, trusted=True)
    spawn = Spawner()
    assert offer_trust(trusted, ask=lambda _p: True, spawn=spawn, which=_which(True)) == TRUSTED
    absent = ProbeTarget("muse", "muse", tmp_path / "nowhere" / "muse-probe", None)
    assert (
        offer_trust(absent, ask=lambda _p: True, spawn=spawn, which=_which(False)) == NOT_INSTALLED
    )
    assert not absent.folder.exists() and spawn.calls == []


def test_doctor_lines_cover_each_state(tmp_path: Path) -> None:
    trusted = _target(tmp_path, trusted=True)
    other = ProbeTarget("muse", "muse", tmp_path / "other", trusted.trust_store)
    assert probe_state(trusted, _which(True)) == TRUSTED
    assert probe_state(other, _which(True)) == NOT_TRUSTED
    assert probe_state(trusted, _which(False)) == NOT_INSTALLED
    assert doctor_line(trusted, TRUSTED) == f"muse probe folder {trusted.folder}: trusted"
    assert doctor_line(other, NOT_TRUSTED) == (
        f"muse probe folder {other.folder}: not trusted (run: relaydeck-companion usage install)"
    )
    assert (
        doctor_line(other, NOT_INSTALLED) == f"muse probe folder {other.folder}: muse not installed"
    )


def _args(action: str, **over: Any) -> argparse.Namespace:
    base = {
        "usage_action": action,
        "source": None,
        "yes": False,
        "install_limits_hook": False,
        "create_settings": False,
        "replace_statusline": False,
    }
    return argparse.Namespace(**(base | over))


def test_doctor_reports_the_probe_folder_and_writes_nothing(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    target = _target(tmp_path)
    store = Path(str(target.trust_store))
    before = store.read_bytes(), store.stat().st_mtime_ns
    monkeypatch.setattr("relaydeck.usage.probe_setup.shutil.which", _which(True))
    assert hook_cli.run_usage(_args("doctor"), [], probes=[target]) == 0
    out = capsys.readouterr()
    assert json.loads(out.out)["probe_folders"] == [
        {"tool": "muse", "path": str(target.folder), "state": NOT_TRUSTED}
    ]
    assert "not trusted (run: relaydeck-companion usage install)" in out.err
    assert (store.read_bytes(), store.stat().st_mtime_ns) == before
    assert not target.folder.exists()  # doctor is read only


def test_install_without_a_source_runs_only_the_probe_step_without_a_terminal(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    target = _target(tmp_path)
    monkeypatch.setattr("relaydeck.usage.probe_setup.shutil.which", _which(True))
    monkeypatch.setattr("sys.stdin", SimpleNamespace(isatty=lambda: False))
    assert hook_cli.run_usage(_args("install"), [], probes=[target]) == 0
    assert target.folder.is_dir()
    assert "choose Trust" in capsys.readouterr().err


def test_repair_offers_the_step_only_for_an_untrusted_folder(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    asked: list[str] = []
    monkeypatch.setattr("relaydeck.usage.probe_setup.shutil.which", _which(True))
    monkeypatch.setattr("sys.stdin", SimpleNamespace(isatty=lambda: True))
    monkeypatch.setattr(hook_cli, "ask_on_terminal", lambda p: asked.append(p) or False)
    hook_cli.run_usage(_args("repair"), [], probes=[_target(tmp_path)])
    assert len(asked) == 1
    asked.clear()
    (tmp_path / "t").mkdir()
    hook_cli.run_usage(_args("repair"), [], probes=[_target(tmp_path / "t", trusted=True)])
    assert asked == []


def test_probe_targets_come_from_enabled_limits_blocks_only(tmp_path: Path) -> None:
    config = tmp_path / "config.toml"
    config.write_text(
        f'state_dir = "{tmp_path}/st"\n'
        '[[usage.limits]]\nkind = "muse"\nformat = "tui-usage-pane-v1"\n'
        '[[usage.limits]]\nkind = "kiro"\nformat = "tui-usage-pane-v1"\n'
    )
    targets = load_probe_targets(config)
    assert [(t.kind, t.folder) for t in targets] == [("muse", tmp_path / "st" / "muse-probe")]
    config.write_text(config.read_text().replace('kind = "muse"', 'kind = "muse"\nenabled = false'))
    assert load_probe_targets(config) == ()
    assert load_probe_targets(tmp_path / "missing.toml") == ()


# -- the Usage view's hint ---------------------------------------------------------


def _refresh(result: str, probe: object) -> Any:
    return usage_model._refresh(
        {
            "available": True,
            "reason": None,
            "in_progress": False,
            "cooldown_until_ms": None,
            "last_error": "unavailable",
            "last_attempt": {"id": "a1", "finished_ms": 5, "result": result},
            "probe_dir": probe,
        }
    )


def test_the_usage_row_says_where_to_trust_muse_once() -> None:
    folder = "/Users/me/Library/Application Support/RelayDeck/muse-probe"
    view = SimpleNamespace(
        kind="muse", name="Muse", refresh=_refresh("workspace_untrusted", folder)
    )
    note = screen_model._refresh_model(view, None, 10).note  # type: ignore[arg-type, union-attr]
    assert note == f"Trust RelayDeck's Muse folder once: run `muse` in {folder} and choose Trust."
    other = SimpleNamespace(kind="muse", name="Muse", refresh=_refresh("timeout", folder))
    assert "Trust RelayDeck" not in screen_model._refresh_model(other, None, 10).note  # type: ignore[arg-type, union-attr]
    assert _refresh("workspace_untrusted", "relative/x").probe_dir is None
