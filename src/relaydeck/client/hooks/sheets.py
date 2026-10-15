"""Creation-sheet hook of `Root`.

Plain functions called from `Root` at the position the inline code used to occupy, so hook
order and the control tree are unchanged. Each takes only what it reads (keyword-only) and
returns the names the rest of `Root` still uses.
"""

from __future__ import annotations

from typing import Any

import flet as ft

# design tokens (minimal first theme, centrally configurable).


def use_creation_sheets() -> tuple[Any, ...]:
    """Creation sheet state, drafts, shell-origin refs and the launch-alive effect."""
    sheet, set_sheet = ft.use_state("")

    creation_drafts: Any = ft.use_ref({})

    management_fields: Any = ft.use_ref(("", "", ""))

    main_screen_revision, set_main_screen_revision = ft.use_state(0)

    submitting, set_submitting = ft.use_state(False)

    sheet_error, set_sheet_error = ft.use_state("")

    sheet_status, set_sheet_status = ft.use_state("")

    shell_origin: Any = ft.use_ref(None)

    shell_attempts: Any = ft.use_ref({})

    launch_busy: Any = ft.use_ref(False)

    launch_task: Any = ft.use_ref(None)

    launch_alive: Any = ft.use_ref(True)

    def hold_launch() -> None:
        launch_alive.current = True

    def release_launch() -> None:
        launch_alive.current = False
        task, launch_task.current = launch_task.current, None
        if task is not None and not task.done():
            task.cancel()

    ft.use_effect(hold_launch, dependencies=[], cleanup=release_launch)
    return (
        creation_drafts,
        launch_alive,
        launch_busy,
        launch_task,
        main_screen_revision,
        management_fields,
        set_main_screen_revision,
        set_sheet,
        set_sheet_error,
        set_sheet_status,
        set_submitting,
        sheet,
        sheet_error,
        sheet_status,
        shell_attempts,
        shell_origin,
        submitting,
    )
