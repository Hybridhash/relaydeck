"""Creation-flow validation. Pure and shared
by both sheets so agent and shell rules cannot drift."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass

from relaydeck.contracts.launch import agent_name_error, has_control, valid_new_workspace

# Defaults are for standalone validation; Root passes the selected host catalog.
MAX_NAME = 32
MAX_LABEL = 64
MAX_ARGV_ITEMS = 32
MAX_ARGV_ITEM_CHARS = 512


@dataclass(frozen=True)
class AgentDraft:
    host_id: str
    workspace_id: str
    name: str
    kind: str
    argv_text: str
    workspace_mode: str = "existing"
    workspace_directory: str = ""
    workspace_label: str = ""


@dataclass(frozen=True)
class ShellDraft:
    host_id: str
    workspace_id: str
    label: str
    workspace_mode: str = "existing"
    workspace_directory: str = ""
    workspace_label: str = ""


def creation_context(
    hosts: tuple[tuple[str, str], ...],
    workspaces: Mapping[str, tuple[tuple[str, str], ...]],
    preferred_host: str = "",
    preferred_workspace: str = "",
) -> tuple[str, str]:
    """Preselect the scoped space filter, or the chosen host's sole space."""
    known = {host for host, _ in hosts}
    host = preferred_host if preferred_host in known else hosts[0][0] if hosts else ""
    choices = tuple(workspace for workspace, _ in workspaces.get(host, ()))
    if host == preferred_host and preferred_workspace in choices:
        return host, preferred_workspace
    return host, choices[0] if len(choices) == 1 else ""


def parse_argv(text: str) -> tuple[list[str], str | None]:
    """Shell-like argv split. Returns (argv, error)."""
    stripped = text.strip()
    if not stripped:
        return [], None
    punctuation: dict[str, str | int | None] = {
        "‘": "'",
        "’": "'",
        "“": '"',
        "”": '"',
        "—": "--",
        "–": "-",
    }
    normalized = stripped.translate(str.maketrans(punctuation))
    argv: list[str] = []
    current = ""
    quote = ""
    escaping = active = False
    for char in normalized:
        if escaping:
            if has_control(char):
                return [], "Arguments contain an unsupported control character."
            current += char
            active, escaping = True, False
        elif quote:
            if char == quote:
                quote = ""
            elif char == "\\" and quote == '"':
                escaping = True
            elif has_control(char):
                return [], "Arguments contain an unsupported control character."
            else:
                current += char
            active = True
        elif char in ("'", '"'):
            quote, active = char, True
        elif char == "\\":
            escaping, active = True, True
        elif char.isspace():
            if active:
                argv.append(current)
                current, active = "", False
        elif has_control(char):
            return [], "Arguments contain an unsupported control character."
        else:
            current += char
            active = True
    if quote or escaping:
        return [], "Could not parse arguments (check quotes)."
    if active:
        argv.append(current)
    if len(argv) > MAX_ARGV_ITEMS:
        return [], f"At most {MAX_ARGV_ITEMS} arguments."
    for item in argv:
        if len(item) > MAX_ARGV_ITEM_CHARS:
            return [], "One argument is too long."
    return argv, None


def _check_host_workspace(
    errors: dict[str, str],
    host_id: str,
    workspace_id: str,
    host_ids: frozenset[str],
    workspace_ids_by_host: Mapping[str, frozenset[str]],
) -> None:
    if host_id not in host_ids:
        errors["host"] = "Choose a host."
        return
    if workspace_id not in workspace_ids_by_host.get(host_id, frozenset()):
        errors["workspace"] = "Choose a workspace."


def _check_destination(
    errors: dict[str, str],
    draft: AgentDraft | ShellDraft,
    host_ids: frozenset[str],
    workspace_ids_by_host: Mapping[str, frozenset[str]],
) -> None:
    if draft.workspace_mode == "existing":
        _check_host_workspace(
            errors, draft.host_id, draft.workspace_id, host_ids, workspace_ids_by_host
        )
    elif draft.workspace_mode == "new":
        if draft.host_id not in host_ids:
            errors["host"] = "Choose a host."
        if not valid_new_workspace(
            {"directory": draft.workspace_directory.strip(), "label": draft.workspace_label.strip()}
        ):
            errors["workspace_directory"] = (
                "Use an absolute directory and a label of at most 64 characters."
            )
    else:
        errors["workspace"] = "Choose an existing or new workspace."


def validate_agent(
    draft: AgentDraft,
    *,
    host_ids: frozenset[str],
    workspace_ids_by_host: Mapping[str, frozenset[str]],
    kinds: tuple[str, ...],
) -> dict[str, str]:
    errors: dict[str, str] = {}
    _check_destination(errors, draft, host_ids, workspace_ids_by_host)
    if draft.name and (error := agent_name_error(draft.name)):
        errors["name"] = error
    if draft.kind not in kinds:
        errors["kind"] = "Choose an agent kind."
    _, argv_error = parse_argv(draft.argv_text)
    if argv_error is not None:
        errors["argv"] = argv_error
    return errors


def validate_shell(
    draft: ShellDraft,
    *,
    host_ids: frozenset[str],
    workspace_ids_by_host: Mapping[str, frozenset[str]],
) -> dict[str, str]:
    errors: dict[str, str] = {}
    _check_destination(errors, draft, host_ids, workspace_ids_by_host)
    if len(draft.label) > MAX_LABEL:
        errors["label"] = f"Label must be at most {MAX_LABEL} characters."
    elif has_control(draft.label):
        errors["label"] = "Label contains an unsupported control character."
    return errors


def open_terminal_behavior(shell_tab_ids: tuple[str, ...]) -> str:
    """Open Terminal from Agent with zero, one or several
    ordinary shells produces create, open or chooser behavior."""
    if len(shell_tab_ids) == 0:
        return "create"
    if len(shell_tab_ids) == 1:
        return "open"
    return "choose"
