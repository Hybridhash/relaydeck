"""Ancestry and shape helpers for the phone terminal."""

from __future__ import annotations

from typing import Any


def _ancestry(app: Any, target: Any) -> tuple[Any, ...]:
    def walk(node: Any, path: tuple[Any, ...]) -> tuple[Any, ...] | None:
        if isinstance(node, list | tuple):
            for child in node:
                found = walk(child, path)
                if found is not None:
                    return found
            return None
        if node is None:
            return None
        path = (*path, node)
        if node is target:
            return path
        for attr in ("_b", "controls", "content"):
            found = walk(getattr(node, attr, None), path)
            if found is not None:
                return found
        return None

    found = walk(app.page.views[0].controls, ())
    assert found is not None
    return found


def _shape(path: tuple[Any, ...]) -> tuple[Any, ...]:
    return tuple(
        (
            type(node),
            node._i,
            node.key,
            getattr(node, "visible", None),
            getattr(node, "opacity", None),
            getattr(node, "ignore_interactions", None),
            *(getattr(node, attr, None) for attr in ("left", "right", "top", "bottom")),
        )
        for node in path
    )
