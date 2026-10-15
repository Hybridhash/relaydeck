"""Stale-draft guard shared by the controlled draft text fields.

Flet applies native text edits (paste, typing, autocorrect) to the field before it
dispatches `on_change`. A parent render between those two messages must not send the
older draft back into the field, or the native edit is lost. The guard keeps the native
value while the model has not moved since the last render; a model change from the
server (send cleared it, restore, another device) renders the model value.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

import flet as ft


def keep_native_value(previous: Any, last_model: str | None, draft: str, *, focusable: bool) -> Any:
    """The value a render should send: the native field's value while the model has not
    moved and the field can take focus, otherwise the model draft."""
    if previous is not None and last_model == draft and previous.can_request_focus and focusable:
        return previous.value
    return draft


def use_draft_value(draft: str, *, focusable: bool) -> tuple[str, Callable[[Any], None]]:
    """Hook: the value to render for `draft`, and a setter to call with the rendered field.

    Call the setter with the field after building it so the next render can compare.
    """
    field_ref: Any = ft.use_ref(None)
    model_ref = ft.use_ref(draft)
    value = keep_native_value(field_ref.current, model_ref.current, draft, focusable=focusable)

    def remember(field: Any) -> None:
        field_ref.current = field
        model_ref.current = draft

    return value, remember
