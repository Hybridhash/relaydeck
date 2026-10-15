"""Creation sheets.

Two strictly separate flows: Start Agent (agent.start with host,
workspace, kind, argv and name) and New Terminal (shell.create with
host, workspace and label). Drafts live in the sheet while open;
validation is pure (`validation.py`); submission hands a validated
draft to the parent, which maps it to a command (`commands.py`).
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import Any

import flet as ft

from relaydeck.client.features.create.validation import (
    AgentDraft,
    ShellDraft,
    creation_context,
    parse_argv,
    validate_agent,
    validate_shell,
)
from relaydeck.client.features.terminal.events import event_text
from relaydeck.client.theme import FONT_SIZE, MIN_TOUCH, RADIUS, palette
from relaydeck.contracts.launch import default_agent_name

START_AGENT_SHEET_KEY = "start-agent-sheet"
NEW_TERMINAL_SHEET_KEY = "new-terminal-sheet"
SHEET_FRAME_KEY = "sheet-frame"
SHEET_ACTIONS_KEY = "sheet-actions"


@ft.component
def CreationSheetCard(body: Any, width: float, max_height: float) -> Any:
    """Measure the natural body under the scroll view, then cap the card.

    Pinned Flet's scroll Column fills its incoming height and ListView has
    no public shrink-wrap switch. Measuring the non-flex child also avoids
    asking Material dropdowns for unsupported intrinsic layout dimensions.
    """
    initial_height: float | None = None
    body_height, set_body_height = ft.use_state(initial_height)

    def measure(event: ft.LayoutSizeChangeEvent[ft.LayoutControl]) -> None:
        if 0 <= event.height < float("inf"):
            set_body_height(event.height)

    return ft.Container(
        key="sheet-card",
        width=width,
        height=min(max_height, body_height + 32) if body_height is not None else None,
        bgcolor=ft.Colors.SURFACE,
        border_radius=RADIUS[2],
        padding=16,
        content=ft.Column(
            tight=True,
            scroll=ft.ScrollMode.AUTO,
            controls=[ft.Container(key="sheet-natural-body", on_size_change=measure, content=body)],
        ),
    )


def _default_name(
    kind: str,
    host_id: str,
    names: Mapping[str, frozenset[str]] | None,
) -> str:
    return (
        default_agent_name(kind, names.get(host_id, frozenset()) if names else ()) if kind else ""
    )


def _workspace_controls(
    prefix: str,
    mode: str,
    set_mode: Callable[[str], None],
    directory: str,
    set_directory: Callable[[str], None],
    label: str,
    set_label: Callable[[str], None],
    errors: dict[str, str],
) -> list[Any]:
    return [
        _dropdown(
            key=f"{prefix}-workspace-mode",
            label="Launch target",
            value=mode,
            options=[
                ft.DropdownOption(key="existing", text="Existing workspace"),
                ft.DropdownOption(key="new", text="New workspace"),
            ],
            on_select=lambda e: set_mode(event_text(e)),
        ),
        ft.TextField(
            key=f"{prefix}-workspace-directory",
            label="Directory (optional)",
            value=directory,
            content_padding=ft.Padding.symmetric(horizontal=12, vertical=16),
            visible=mode == "new",
            hint_text="Home directory when empty",
            error=errors.get("workspace_directory"),
            on_change=lambda e: set_directory(event_text(e)),
        ),
        ft.TextField(
            key=f"{prefix}-workspace-label",
            label="Workspace label (optional)",
            value=label,
            content_padding=ft.Padding.symmetric(horizontal=12, vertical=16),
            visible=mode == "new",
            on_change=lambda e: set_label(event_text(e)),
        ),
    ]


def _dropdown(**props: Any) -> ft.Row:
    """DropdownMenu fills a bounded horizontal row, with natural label height."""
    visible = props.pop("visible", True)
    return ft.Row(
        visible=visible,
        controls=[
            ft.Dropdown(
                visible=visible,
                expand=True,
                expanded_insets=0,
                content_padding=ft.Padding.symmetric(horizontal=12, vertical=16),
                **props,
            )
        ],
    )


def _framed(tokens: dict[str, str], controls: list[Any]) -> Any:
    """The Root card owns the frame; isolate dropdown arrows from chrome fill."""
    return ft.Container(
        key=SHEET_FRAME_KEY,
        theme=ft.Theme(
            icon_button_theme=ft.IconButtonTheme(
                style=ft.ButtonStyle(
                    bgcolor=ft.Colors.TRANSPARENT,
                    side=ft.BorderSide(0, ft.Colors.TRANSPARENT),
                    color=tokens["text"],
                )
            )
        ),
        content=ft.Column(
            tight=True,
            spacing=16,
            horizontal_alignment=ft.CrossAxisAlignment.STRETCH,
            controls=controls,
        ),
    )


@ft.component
def StartAgentSheet(
    hosts: tuple[tuple[str, str], ...],
    workspaces_by_host: Mapping[str, tuple[tuple[str, str], ...]],
    kinds: tuple[str, ...],
    submitting: bool,
    error: str,
    command_status: str,
    on_submit: Callable[[AgentDraft, list[str]], Any],
    on_cancel: Callable[[], Any],
    dark: bool = True,
    kind_catalogs: Mapping[str, tuple[str, ...]] | None = None,
    existing_names_by_host: Mapping[str, frozenset[str]] | None = None,
    initial_draft: AgentDraft | None = None,
    on_draft_change: Callable[[AgentDraft], None] | None = None,
    preferred_host: str = "",
    preferred_workspace: str = "",
) -> Any:
    """Full-width, naturally sized fields inside the Root-owned sheet card."""
    default_host, default_workspace = creation_context(
        hosts, workspaces_by_host, preferred_host, preferred_workspace
    )
    host_id, set_host = ft.use_state(initial_draft.host_id if initial_draft else default_host)
    workspace_id, set_workspace = ft.use_state(
        initial_draft.workspace_id if initial_draft else default_workspace
    )
    workspace_mode, set_workspace_mode = ft.use_state(
        initial_draft.workspace_mode if initial_draft else "existing"
    )
    directory, set_directory = ft.use_state(
        initial_draft.workspace_directory if initial_draft else ""
    )
    workspace_label, set_workspace_label = ft.use_state(
        initial_draft.workspace_label if initial_draft else ""
    )
    name, set_name = ft.use_state(initial_draft.name if initial_draft else "")
    initial_kinds = kind_catalogs.get(host_id, ()) if kind_catalogs is not None else kinds
    kind, set_kind = ft.use_state(
        initial_draft.kind if initial_draft else initial_kinds[0] if initial_kinds else ""
    )
    argv_text, set_argv = ft.use_state(initial_draft.argv_text if initial_draft else "")
    errors, set_errors = ft.use_state(dict[str, str]())
    draft = AgentDraft(
        host_id, workspace_id, name, kind, argv_text, workspace_mode, directory, workspace_label
    )

    def remember() -> None:
        if on_draft_change is not None:
            on_draft_change(draft)

    ft.use_effect(remember, dependencies=[draft, on_draft_change])

    def pick_host(value: str) -> None:
        set_host(value)
        # a host change clears the workspace choice so a
        # stale workspace can never be submitted.
        set_workspace("")
        set_workspace_mode("existing")
        set_directory("")
        set_workspace_label("")
        next_kinds = kind_catalogs.get(value, ()) if kind_catalogs is not None else kinds
        set_kind(next_kinds[0] if next_kinds else "")

    kinds = kind_catalogs.get(host_id, ()) if kind_catalogs is not None else kinds

    def attempt() -> None:
        found = validate_agent(
            AgentDraft(
                host_id=host_id,
                workspace_id=workspace_id,
                name=name,
                kind=kind,
                argv_text=argv_text,
                workspace_mode=workspace_mode,
                workspace_directory=directory,
                workspace_label=workspace_label,
            ),
            host_ids=frozenset(h for h, _ in hosts),
            workspace_ids_by_host={
                h: frozenset(w for w, _ in workspaces_by_host.get(h, ())) for h, _ in hosts
            },
            kinds=kinds,
        )
        set_errors(found)
        if not found and not submitting:
            argv, _ = parse_argv(argv_text)
            on_submit(
                AgentDraft(
                    host_id=host_id,
                    workspace_id=workspace_id,
                    name=name.strip(),
                    kind=kind,
                    argv_text=argv_text,
                    workspace_mode=workspace_mode,
                    workspace_directory=directory,
                    workspace_label=workspace_label,
                ),
                argv,
            )

    workspaces = workspaces_by_host.get(host_id, ())
    tokens = palette(dark)
    return ft.Column(
        key=START_AGENT_SHEET_KEY,
        tight=True,
        horizontal_alignment=ft.CrossAxisAlignment.STRETCH,
        spacing=16,
        controls=[
            _framed(
                tokens,
                [
                    ft.Text(
                        "Start agent",
                        key="sheet-title",
                        size=FONT_SIZE["headline"],
                        weight=ft.FontWeight.BOLD,
                    ),
                    _dropdown(
                        key="agent-host",
                        label="Host",
                        value=host_id,
                        options=[ft.DropdownOption(text=label, key=hid) for hid, label in hosts],
                        error_text=errors.get("host"),
                        on_select=lambda e: pick_host(event_text(e)),
                    ),
                    _dropdown(
                        key="agent-workspace",
                        label="Workspace",
                        value=workspace_id,
                        visible=workspace_mode == "existing",
                        options=[
                            ft.DropdownOption(text=label, key=wid) for wid, label in workspaces
                        ],
                        error_text=errors.get("workspace"),
                        on_select=lambda e: set_workspace(event_text(e)),
                    ),
                    *_workspace_controls(
                        "agent",
                        workspace_mode,
                        set_workspace_mode,
                        directory,
                        set_directory,
                        workspace_label,
                        set_workspace_label,
                        errors,
                    ),
                    ft.TextField(
                        key="agent-name",
                        label="Name (optional)",
                        hint_text=_default_name(kind, host_id, existing_names_by_host),
                        value=name,
                        content_padding=ft.Padding.symmetric(horizontal=12, vertical=16),
                        error=errors.get("name"),
                        on_change=lambda e: set_name(event_text(e)),
                    ),
                    _dropdown(
                        key="agent-kind",
                        label="Agent",
                        value=kind,
                        options=[ft.DropdownOption(text=k.capitalize(), key=k) for k in kinds],
                        error_text=errors.get("kind"),
                        on_select=lambda e: set_kind(event_text(e)),
                        disabled=not kinds,
                    ),
                    ft.TextField(
                        key="agent-argv",
                        label="Arguments (optional)",
                        value=argv_text,
                        content_padding=ft.Padding.symmetric(horizontal=12, vertical=16),
                        error=errors.get("argv"),
                        on_change=lambda e: set_argv(event_text(e)),
                    ),
                    ft.Text(command_status, key="sheet-status", visible=bool(command_status)),
                    ft.Text(
                        "No agent kinds are configured on this Mac. "
                        "Add agent_kinds to companion.toml.",
                        key="agent-catalog-empty",
                        visible=not kinds,
                    ),
                    ft.Text(error, key="sheet-error", visible=bool(error)),
                    ft.ProgressRing(key="sheet-spinner", visible=submitting),
                    ft.Row(
                        key=SHEET_ACTIONS_KEY,
                        alignment=ft.MainAxisAlignment.END,
                        controls=[
                            ft.Button(
                                "Cancel",
                                key="sheet-cancel",
                                height=MIN_TOUCH,
                                expand=True,
                                disabled=submitting,
                                on_click=lambda e: on_cancel(),
                            ),
                            ft.Button(
                                "Start",
                                key="sheet-submit",
                                disabled=submitting or not kinds,
                                height=MIN_TOUCH,
                                expand=True,
                                on_click=lambda e: attempt(),
                            ),
                        ],
                    ),
                ],
            ),
        ],
    )


@ft.component
def NewTerminalSheet(
    hosts: tuple[tuple[str, str], ...],
    workspaces_by_host: Mapping[str, tuple[tuple[str, str], ...]],
    submitting: bool,
    error: str,
    command_status: str,
    on_submit: Callable[[ShellDraft], Any],
    on_cancel: Callable[[], Any],
    dark: bool = True,
    initial_draft: ShellDraft | None = None,
    on_draft_change: Callable[[ShellDraft], None] | None = None,
    preferred_host: str = "",
    preferred_workspace: str = "",
) -> Any:
    """Full-width, naturally sized fields inside the Root-owned sheet card."""
    default_host, default_workspace = creation_context(
        hosts, workspaces_by_host, preferred_host, preferred_workspace
    )
    host_id, set_host = ft.use_state(initial_draft.host_id if initial_draft else default_host)
    workspace_id, set_workspace = ft.use_state(
        initial_draft.workspace_id if initial_draft else default_workspace
    )
    workspace_mode, set_workspace_mode = ft.use_state(
        initial_draft.workspace_mode if initial_draft else "existing"
    )
    directory, set_directory = ft.use_state(
        initial_draft.workspace_directory if initial_draft else ""
    )
    workspace_label, set_workspace_label = ft.use_state(
        initial_draft.workspace_label if initial_draft else ""
    )
    label, set_label = ft.use_state(initial_draft.label if initial_draft else "")
    errors, set_errors = ft.use_state(dict[str, str]())
    saved_draft = ShellDraft(
        host_id, workspace_id, label, workspace_mode, directory, workspace_label
    )

    def remember() -> None:
        if on_draft_change is not None:
            on_draft_change(saved_draft)

    ft.use_effect(remember, dependencies=[saved_draft, on_draft_change])

    def pick_host(value: str) -> None:
        set_host(value)
        set_workspace("")
        set_workspace_mode("existing")
        set_directory("")
        set_workspace_label("")

    def attempt() -> None:
        draft = ShellDraft(
            host_id=host_id,
            workspace_id=workspace_id,
            label=label,
            workspace_mode=workspace_mode,
            workspace_directory=directory,
            workspace_label=workspace_label,
        )
        found = validate_shell(
            draft,
            host_ids=frozenset(h for h, _ in hosts),
            workspace_ids_by_host={
                h: frozenset(w for w, _ in workspaces_by_host.get(h, ())) for h, _ in hosts
            },
        )
        set_errors(found)
        if not found and not submitting:
            on_submit(draft)

    workspaces = workspaces_by_host.get(host_id, ())
    tokens = palette(dark)
    return ft.Column(
        key=NEW_TERMINAL_SHEET_KEY,
        tight=True,
        horizontal_alignment=ft.CrossAxisAlignment.STRETCH,
        spacing=16,
        controls=[
            _framed(
                tokens,
                [
                    ft.Text(
                        "New terminal",
                        key="sheet-title",
                        size=FONT_SIZE["headline"],
                        weight=ft.FontWeight.BOLD,
                    ),
                    _dropdown(
                        key="shell-host",
                        label="Host",
                        value=host_id,
                        options=[ft.DropdownOption(text=label_, key=hid) for hid, label_ in hosts],
                        error_text=errors.get("host"),
                        on_select=lambda e: pick_host(event_text(e)),
                    ),
                    _dropdown(
                        key="shell-workspace",
                        label="Workspace",
                        value=workspace_id,
                        visible=workspace_mode == "existing",
                        options=[
                            ft.DropdownOption(text=label_, key=wid) for wid, label_ in workspaces
                        ],
                        error_text=errors.get("workspace"),
                        on_select=lambda e: set_workspace(event_text(e)),
                    ),
                    *_workspace_controls(
                        "shell",
                        workspace_mode,
                        set_workspace_mode,
                        directory,
                        set_directory,
                        workspace_label,
                        set_workspace_label,
                        errors,
                    ),
                    ft.TextField(
                        key="shell-label",
                        label="Tab label (optional)",
                        value=label,
                        content_padding=ft.Padding.symmetric(horizontal=12, vertical=16),
                        error=errors.get("label"),
                        on_change=lambda e: set_label(event_text(e)),
                    ),
                    ft.Text(command_status, key="sheet-status", visible=bool(command_status)),
                    ft.Text(error, key="sheet-error", visible=bool(error)),
                    ft.ProgressRing(key="sheet-spinner", visible=submitting),
                    ft.Row(
                        key=SHEET_ACTIONS_KEY,
                        alignment=ft.MainAxisAlignment.END,
                        controls=[
                            ft.Button(
                                "Cancel",
                                key="sheet-cancel",
                                height=MIN_TOUCH,
                                expand=True,
                                disabled=submitting,
                                on_click=lambda e: on_cancel(),
                            ),
                            ft.Button(
                                "Create",
                                key="sheet-submit",
                                disabled=submitting,
                                height=MIN_TOUCH,
                                expand=True,
                                on_click=lambda e: attempt(),
                            ),
                        ],
                    ),
                ],
            ),
        ],
    )
