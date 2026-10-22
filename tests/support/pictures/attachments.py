"""Picture picker stubs and constants."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import flet as ft
import pytest

from relaydeck.client.features.shell.ops import ShellGatewayOps
from tests.support.paths import TESTS_DIR

PATH = "/Users/synthetic/Library/Application Support/RelayDeck/attachments/abcdef.jpg"


IDENTIFIER = "a" * 32


PREVIEW = json.loads((TESTS_DIR / "fixtures/gateway/picture-result.json").read_text())["preview"]


class _Api:
    def __init__(self) -> None:
        self.cancelled: list[str] = []
        self.failure = False

    async def begin(self, host: str) -> dict[str, Any]:
        if self.failure:
            raise RuntimeError("PlatformException(secret=/Users/private/not-for-ui)")
        return {
            "picture_id": IDENTIFIER,
            "upload_url": "/v1/pictures/synthetic/content?grant=synthetic",
        }

    async def result(self, identifier: str) -> dict[str, Any]:
        return {
            "picture_id": identifier,
            "status": "ready",
            "path": PATH,
            "expires_ms": 99_999_999_999_999,
            "preview": PREVIEW,
        }

    async def cancel(self, identifier: str) -> None:
        self.cancelled.append(identifier)


def install_picker(monkeypatch: pytest.MonkeyPatch, api: _Api, on_upload: Any = None) -> list[Any]:
    calls: list[Any] = []
    monkeypatch.setattr(ShellGatewayOps, "picture_api", property(lambda _: api))

    async def pick(self: Any, **kwargs: Any) -> list[Any]:
        raise AssertionError("iOS picker must open through the native gesture action")

    async def upload(self: Any, files: Any) -> None:
        calls.append(files)
        await self.page.session.dispatch_event(
            self._i,
            "upload",
            {
                "file_name": "spoof.exe",
                "progress": 0.5,
                "error": None,
            },
        )
        if on_upload:
            await on_upload()
        await self.page.session.dispatch_event(
            self._i,
            "upload",
            {
                "file_name": "spoof.exe",
                "progress": 1.0,
                "error": None,
            },
        )

    monkeypatch.setattr(ft.FilePicker, "pick_files", pick)
    monkeypatch.setattr(ft.FilePicker, "upload", upload)
    return calls


SHELL_ATTACH = "terminal-attach-picture"
TOOLS_ATTACH = "tools-attach-picture"


def attach_key(app: Any, key: str = SHELL_ATTACH) -> str:
    """The attach control this screen has: the shell's (or wide layout's) own button when it
    exists, otherwise the phone agent's action in the tools popover."""
    try:
        app.find(key)
    except KeyError:
        if key == SHELL_ATTACH:
            return TOOLS_ATTACH
    return key


def attach_control(app: Any, key: str = SHELL_ATTACH) -> Any:
    return app.find(attach_key(app, key))


async def click_attach(app: Any, key: str = SHELL_ATTACH) -> None:
    """Click attach; on the phone agent screen that means tools button, then the action."""
    real = attach_key(app, key)
    if real == TOOLS_ATTACH:
        await app.set_props_and_fire("terminal-tools-button", {}, "click")
    await app.set_props_and_fire(real, {}, "click")


async def choose(app: Any, key: str, path: Path | None = None) -> None:
    action = attach_control(app, key).action
    assert isinstance(action, ft.PickFiles)
    assert action.allow_multiple is False and action.file_type is ft.FilePickerFileType.IMAGE
    await click_attach(app, key)
    # The native action's pinned result payload follows its click event.
    await app.session.dispatch_event(
        action.file_picker._i,
        "result",
        {
            "files": [
                {
                    "id": 7,
                    "name": "spoof.exe",
                    "size": path.stat().st_size if path else 100,
                    **({"path": str(path)} if path else {}),
                }
            ],
        },
    )
    await app.settle()
