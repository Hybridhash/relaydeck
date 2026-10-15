"""Control-tree walking for identity tests: ancestors of a key and Flutter wrapper signatures."""

from __future__ import annotations

from typing import Any


def keys_of(control: Any) -> list[Any]:
    return [child.key for child in control.controls]


def children_of(control: Any) -> list[Any]:
    out: list[Any] = []
    body = getattr(control, "_b", None)
    if body is not None:
        out.extend(body if isinstance(body, list) else [body])
    for attr in ("controls", "content", "actions", "leading", "trailing"):
        value = getattr(control, attr, None)
        if isinstance(value, list | tuple):
            out.extend(value)
        elif value is not None:
            out.append(value)
    return out


def _path_to(app: Any, key: str) -> list[Any]:
    """Ancestors (outermost first) of the control with `key`, inclusive."""
    root = app.page.views[0].controls

    def walk(node: Any, trail: list[Any]) -> list[Any] | None:
        if node is None:
            return None
        here = [*trail, node]
        if getattr(node, "key", None) == key:
            return here
        for child in children_of(node):
            found = walk(child, here)
            if found:
                return found
        return None

    found = walk(root, [])
    if found:
        return found
    raise KeyError(key)


# Flet wraps a control in a different Flutter widget depending on whether these
# props are set (Positioned, SizedBox, Container-for-margin, Expanded, ...). A
# wrapper that appears or disappears changes the widget structure, which
# remounts everything below it, so presence must not change between layouts.
_WRAPPER_PROPS = (
    "margin",
    "expand",
    "opacity",
    "rotate",
    "scale",
    "offset",
    "aspect_ratio",
    "tooltip",
    "badge",
    "on_size_change",
    "animate_position",
)


def _signature(path: list[Any]) -> list[tuple[Any, ...]]:
    rows: list[tuple[Any, ...]] = []
    for node in path:
        positioned = any(
            getattr(node, a, None) is not None for a in ("left", "top", "right", "bottom")
        )
        sized = any(getattr(node, a, None) is not None for a in ("width", "height"))
        rows.append(
            (
                type(node).__name__,
                getattr(node, "key", None),
                positioned,
                sized,
                # `visible=False` may swap the child for an empty box, so it is part of
                # the structure too: a toggle on any ancestor remounts the field.
                getattr(node, "visible", None),
                *(getattr(node, name, None) is not None for name in _WRAPPER_PROPS),
            )
        )
    return rows
