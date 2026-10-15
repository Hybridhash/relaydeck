"""the companion start path points FLET_WEB_PATH at the newest verified installed
bundle when it is unset; an explicit value always wins; nothing installed prints one hint.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import pytest

from relaydeck import webbundle
from relaydeck.companion import cli as companion_cli


def _unset(monkeypatch: Any) -> None:
    # set-then-delete so monkeypatch restores the original (absent) state afterwards
    monkeypatch.setenv("FLET_WEB_PATH", "placeholder")
    monkeypatch.delenv("FLET_WEB_PATH")


def test_unset_env_uses_the_current_installed_bundle(
    tmp_path: Path, monkeypatch: Any, capsys: pytest.CaptureFixture[str]
) -> None:
    bundle = tmp_path / "web" / "abc"
    _unset(monkeypatch)
    monkeypatch.setattr(webbundle, "current_bundle", lambda: bundle)
    companion_cli._select_web_bundle()
    assert os.environ["FLET_WEB_PATH"] == str(bundle)
    assert "Web bundle:" in capsys.readouterr().out


def test_an_explicit_env_value_always_wins(tmp_path: Path, monkeypatch: Any) -> None:
    monkeypatch.setenv("FLET_WEB_PATH", "/explicit/web")
    monkeypatch.setattr(webbundle, "current_bundle", lambda: tmp_path / "installed")
    companion_cli._select_web_bundle()
    assert os.environ["FLET_WEB_PATH"] == "/explicit/web"


def test_nothing_installed_keeps_today_and_says_how_to_install(
    monkeypatch: Any, capsys: pytest.CaptureFixture[str]
) -> None:
    _unset(monkeypatch)
    monkeypatch.setattr(webbundle, "current_bundle", lambda: None)
    companion_cli._select_web_bundle()
    assert "FLET_WEB_PATH" not in os.environ
    assert "relaydeck web install" in capsys.readouterr().out
