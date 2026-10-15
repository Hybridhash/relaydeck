"""The application version is defined once and read everywhere else."""

from __future__ import annotations

import tomllib
from pathlib import Path

import pytest

from relaydeck import cli
from relaydeck.companion import cli as companion_cli
from relaydeck.version import __version__

ROOT = Path(__file__).resolve().parents[2]


def _declared() -> str:
    return tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))["project"][
        "version"
    ]


def test_the_runtime_version_is_the_project_version() -> None:
    assert __version__ == _declared()


def test_the_plugin_manifest_carries_the_project_version() -> None:
    manifest = tomllib.loads((ROOT / "plugin" / "herdr-plugin.toml").read_text(encoding="utf-8"))
    assert manifest["version"] == _declared()


@pytest.mark.parametrize("main", [cli.main, companion_cli.main])
def test_both_commands_print_the_version(main, capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as stop:
        main(["--version"])
    assert stop.value.code == 0
    assert capsys.readouterr().out.strip() == f"relaydeck {__version__}"


def test_without_package_metadata_the_staged_stamp_is_used(monkeypatch: pytest.MonkeyPatch) -> None:
    import importlib
    import importlib.metadata as metadata
    import sys
    import types

    from relaydeck import version as module

    def missing(name: str) -> str:
        raise metadata.PackageNotFoundError(name)

    stamp = types.ModuleType("relaydeck._version")
    stamp.VERSION = "9.9.9"  # type: ignore[attr-defined]
    monkeypatch.setattr(metadata, "version", missing)
    monkeypatch.setitem(sys.modules, "relaydeck._version", stamp)
    try:
        assert importlib.reload(module).__version__ == "9.9.9"
        monkeypatch.setitem(sys.modules, "relaydeck._version", None)
        assert importlib.reload(module).__version__ == module.UNKNOWN
    finally:
        monkeypatch.undo()
        importlib.reload(module)
