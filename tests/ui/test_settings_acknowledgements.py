"""Settings lists the open-source software the app ships with."""

from __future__ import annotations

import tomllib
from pathlib import Path
from typing import Any

import flet as ft

from relaydeck.client.features.a11y.focus import iter_controls
from relaydeck.client.features.settings.acknowledgements import ACKNOWLEDGEMENT_GROUPS
from relaydeck.client.features.settings.components import SettingsPage
from relaydeck.client.features.settings.prefs import TerminalPrefs
from tests.support.host_harness import render_component, run

REPO = Path(__file__).resolve().parents[2]


def _texts(control: Any) -> set[str]:
    return {c.value for c in iter_controls([control]) if isinstance(c, ft.Text)}


def _names() -> set[str]:
    return {entry.name.lower() for group in ACKNOWLEDGEMENT_GROUPS for entry in group.entries}


def test_every_entry_names_a_licence() -> None:
    assert ACKNOWLEDGEMENT_GROUPS
    for group in ACKNOWLEDGEMENT_GROUPS:
        assert group.title
        assert group.entries
        for entry in group.entries:
            assert entry.name.strip()
            assert entry.license.strip()


def test_runtime_dependencies_and_fonts_are_listed() -> None:
    pyproject = tomllib.loads((REPO / "pyproject.toml").read_text())
    names = _names()
    for requirement in pyproject["project"]["dependencies"]:
        name = requirement.split("=")[0].split(">")[0].split("<")[0].strip().lower()
        assert name in names, name
    assets = REPO / "src/relaydeck/client/assets/design_system"
    for licence_file in assets.glob("OFL-*.txt"):
        font = licence_file.stem.removeprefix("OFL-").replace(" ", "").lower()
        assert any(font == n.replace(" ", "") for n in names), licence_file.name


def test_acknowledgements_open_from_settings() -> None:
    async def scenario() -> None:
        app = await render_component(
            SettingsPage,
            prefs=TerminalPrefs(),
            on_prefs=lambda value: None,
            phone=False,
            account="synthetic",
            version="0.1.0",
            on_logout=lambda: None,
        )
        assert "Acknowledgements" in _texts(app.find("settings-pane"))
        assert "Apache-2.0" not in _texts(app.find("settings-pane"))
        await app.set_props_and_fire("settings-acknowledgements-toggle", {}, "click")
        opened = _texts(app.find("settings-acknowledgements-list"))
        assert {"Flet", "xterm2", "JetBrains Mono", "OFL-1.1"} <= opened

    run(scenario())
