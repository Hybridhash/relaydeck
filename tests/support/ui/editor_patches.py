"""Resolve pinned Flet patch indexes before allowing independent status animations."""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any

from flet.controls.object_patch import Operation
from flet.messaging.protocol import MessageAction

from tests.support.ui.phone_terminal import _ancestry


def _patch_targets(root: Any, tree: list[Any]) -> dict[int, Any]:
    targets = {tree[0]: root}
    if len(tree) > 1:
        for key, child in tree[1].items():
            value = root[key] if isinstance(root, list | tuple | dict) else getattr(root, key)
            targets.update(_patch_targets(value, child))
    return targets


def assert_only_editor_idle_patches(app: Any, field: Any, messages: Iterable[Any]) -> None:
    """Reject every editor/layout patch; allow only the identified dot and hidden icon bars."""
    icon = app.find("console-loading-icon")
    frame = icon._b
    assert frame.width == frame.height == 16
    assert any(node.visible is False for node in _ancestry(app, icon))
    bars = {bar._i for bar in frame.content.controls}
    assert len(bars) == 4
    for message in messages:
        if message.action != MessageAction.PATCH_CONTROL:
            continue
        body = message.body
        assert body.id != field._i, body
        targets = _patch_targets(app.session.index[body.id], body.patch[0])
        for operation in body.patch[1:]:
            assert len(operation) == 4 and operation[0] == Operation.Replace, body
            _, index, prop, value = operation
            target = targets[index]
            dot = (
                prop == "opacity"
                and (getattr(target, "key", None) or "").startswith("terminal-agent-working-dot-")
                and target.width == target.height == 8
                and value in (0.3, 1.0)
            )
            bar = (
                prop == "height"
                and getattr(target, "_i", None) in bars
                and value in (6.4, 12.8, 8.8, 16.0)
            )
            assert dot or bar, body
