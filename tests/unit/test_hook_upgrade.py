"""`usage install` upgrades our previous hook, and nothing else.

A synthetic previous version is planted in a temporary directory: its bytes, a receipt that
records its hash, and the settings line that the earlier install wrote. The allow-list is
monkeypatched, so the real shipped previous hash is not needed here. Never touches the owner's
CLI settings or ~/.config.
"""

from __future__ import annotations

import hashlib
import json
import os
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

from relaydeck.usage import hook_install
from relaydeck.usage.hook_doctor import doctor_hook
from relaydeck.usage.hook_install import install_hook
from relaydeck.usage.hook_resource import hook_bytes
from relaydeck.usage.hook_setup import HOOK_FILENAME, HOOK_SHA256, managed_command

NOW = 2_000_000
PREVIOUS_BODY = b"# synthetic previous RelayDeck hook\n"
PREVIOUS_SHA256 = hashlib.sha256(PREVIOUS_BODY).hexdigest()
UNKNOWN_BODY = b"# someone else's statusline script\n"


def write(path: Path, value: dict[str, Any]) -> None:
    path.write_text(json.dumps(value))
    path.chmod(0o600)


@dataclass
class Files:
    settings: Path
    cache: Path
    hooks: Path
    python: Path

    @property
    def script(self) -> Path:
        return self.hooks / HOOK_FILENAME

    @property
    def receipt(self) -> Path:
        return self.hooks / (
            "limits-" + hashlib.sha256(os.fsencode(self.settings)).hexdigest() + ".json"
        )

    def install(self, **kwargs: Any) -> Any:
        return install_hook(
            self.settings, self.cache, hooks_dir=self.hooks, interpreter=self.python, **kwargs
        )

    def doctor(self) -> dict[str, Any]:
        return doctor_hook(
            self.settings, self.cache, hooks_dir=self.hooks, interpreter=self.python, now_ms=NOW
        )

    def managed(self) -> dict[str, Any]:
        return {
            "type": "command",
            "command": managed_command(self.script, self.cache, self.python),
        }


@pytest.fixture
def files(tmp_path: Path) -> Files:
    root = tmp_path.resolve()
    root.chmod(0o700)
    settings = root / "source settings.json"
    write(settings, {"unrelated": {"name": "private-marker", "value": [True, None, 7]}})
    python = root / "tool env/python"
    python.parent.mkdir(mode=0o700)
    python.symlink_to(Path(sys.executable))
    return Files(settings, root / "private state/cache.json", root / "hooks", python)


def plant_previous(
    files: Files, monkeypatch: pytest.MonkeyPatch, body: bytes = PREVIOUS_BODY
) -> str:
    """Install normally, then rewrite the script and receipt as the previous version left them."""
    digest = hashlib.sha256(body).hexdigest()
    monkeypatch.setattr(hook_install, "PREVIOUS_HOOK_SHA256", (digest,))
    assert files.install(confirmed=True).state == "installed"
    files.script.write_bytes(body)
    receipt = json.loads(files.receipt.read_text())
    receipt["hash"] = digest
    write(files.receipt, receipt)
    return digest


def snapshot(files: Files) -> dict[str, bytes | None]:
    receipt = files.receipt.read_bytes() if files.receipt.exists() else None
    return {
        "settings": files.settings.read_bytes(),
        "script": files.script.read_bytes() if files.script.exists() else None,
        "receipt": receipt,
    }


# -- the upgrade -----------------------------------------------------------------------


def test_upgrade_from_the_previous_hash_replaces_the_script_and_keeps_the_settings_line(
    files: Files, monkeypatch: pytest.MonkeyPatch
) -> None:
    plant_previous(files, monkeypatch)
    assert files.doctor()["installation"]["state"] == "script_outdated"
    line_before = json.loads(files.settings.read_text())["statusLine"]
    assert line_before == files.managed()

    assert files.install(confirmed=True).state == "installed"

    assert files.script.read_bytes() == hook_bytes()
    assert hashlib.sha256(files.script.read_bytes()).hexdigest() == HOOK_SHA256
    assert json.loads(files.receipt.read_text())["hash"] == HOOK_SHA256
    assert json.loads(files.settings.read_text())["statusLine"] == line_before
    assert json.loads(files.settings.read_text())["unrelated"]["name"] == "private-marker"
    assert files.doctor()["installation"]["state"] == "installed"


def test_the_upgrade_is_idempotent(files: Files, monkeypatch: pytest.MonkeyPatch) -> None:
    plant_previous(files, monkeypatch)
    assert files.install(confirmed=True).state == "installed"
    after_first = snapshot(files)
    assert files.install(confirmed=True).state == "installed"
    assert snapshot(files) == after_first


def test_the_upgrade_writes_only_inside_the_hook_directory_and_settings(
    files: Files, monkeypatch: pytest.MonkeyPatch
) -> None:
    plant_previous(files, monkeypatch)
    before = {p for p in files.hooks.parent.rglob("*")}
    assert files.install(confirmed=True).state == "installed"
    created = {p for p in files.hooks.parent.rglob("*")} - before
    assert all(p.is_relative_to(files.hooks) or p == files.settings for p in created)


# -- what stays a conflict ----------------------------------------------------------------


def test_an_unknown_script_hash_is_a_conflict_and_nothing_changes(
    files: Files, monkeypatch: pytest.MonkeyPatch
) -> None:
    plant_previous(files, monkeypatch)
    files.script.write_bytes(UNKNOWN_BODY)
    before = snapshot(files)
    assert files.install(confirmed=True).state == "conflict"
    assert snapshot(files) == before


def test_a_script_without_a_receipt_is_a_conflict(
    files: Files, monkeypatch: pytest.MonkeyPatch
) -> None:
    plant_previous(files, monkeypatch)
    files.receipt.unlink()
    files.script.write_bytes(PREVIOUS_BODY)
    before = snapshot(files)
    assert files.install(confirmed=True).state == "conflict"
    assert snapshot(files) == before


@pytest.mark.parametrize(
    "change",
    [
        "receipt_hash_is_current",
        "receipt_uninstalled",
        "receipt_other_settings",
        "receipt_other_script",
        "receipt_other_interpreter",
        "receipt_other_cache",
        "statusline_changed_by_owner",
        "statusline_missing",
    ],
)
def test_a_receipt_or_settings_mismatch_is_a_conflict(
    files: Files, monkeypatch: pytest.MonkeyPatch, change: str
) -> None:
    plant_previous(files, monkeypatch)
    receipt = json.loads(files.receipt.read_text())
    settings = json.loads(files.settings.read_text())
    if change == "receipt_hash_is_current":
        receipt["hash"] = HOOK_SHA256  # the receipt does not record the previous version
    elif change == "receipt_uninstalled":
        receipt["state"] = "uninstalled"
    elif change == "receipt_other_settings":
        receipt["settings"] = str(files.settings.parent / "other.json")
    elif change == "receipt_other_script":
        receipt["script"] = str(files.hooks.parent / "elsewhere.py")
    elif change == "receipt_other_interpreter":
        receipt["interpreter"] = str(files.python.parent / "other-python")
    elif change == "receipt_other_cache":
        receipt["cache"] = str(files.cache.parent / "other.json")
    elif change == "statusline_changed_by_owner":
        settings["statusLine"] = {"type": "command", "command": "owner-command"}
    elif change == "statusline_missing":
        settings.pop("statusLine")
    write(files.receipt, receipt)
    write(files.settings, settings)
    before = snapshot(files)
    # A receipt whose paths no longer match its managed command fails the existing receipt check
    # ("invalid"); every other mismatch is "conflict". Both refuse, and neither changes a file.
    assert files.install(confirmed=True).state in {"conflict", "invalid"}
    assert snapshot(files) == before


def test_the_allow_list_only_contains_the_previous_hashes_by_default() -> None:
    from relaydeck.usage.hook_setup import PREVIOUS_HOOK_SHA256

    assert PREVIOUS_HOOK_SHA256 == (
        "eece0979aa2ccd21be47121fd2ba2a958a7a2caf8ccf4c8a3b8b24c7995c0c64",
        "f5632268feff5fa6ee8ec65e97049bbbd30f3f42f8000d977267fced84f0e5e7",
        "81833a623b5e14b8b0cf242d87ffdbe3fafb01ceee87e06845bc946fcde4914c",
        "1cf6b549e22c8d0bc1efaf245da1630dbb242ce35c6acc85225b2909eef4b9a6",
        "bc6f77c25219ec3b759df89de39b39960ee3c35b3393b8ad39b3131eb56e59eb",
    )
    assert HOOK_SHA256 not in PREVIOUS_HOOK_SHA256
    assert PREVIOUS_SHA256 not in PREVIOUS_HOOK_SHA256  # the test plants a synthetic one
