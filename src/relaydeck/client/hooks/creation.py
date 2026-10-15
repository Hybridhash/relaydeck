"""Creation action builders of `Root`: agent/shell creation and shell-origin choice.

Plain functions called from `Root` at the position the inline code used to occupy, so hook
order and the control tree are unchanged. Each takes only what it reads (keyword-only) and
returns the names the rest of `Root` still uses.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from typing import Any

from relaydeck.client.features.composer.observation import CommandOutcomeUnknown, CommandRefused
from relaydeck.client.features.create.commands import (
    agent_start_command,
    shell_create_command,
)
from relaydeck.client.features.create.observation import CreationObserver
from relaydeck.client.features.create.validation import (
    AgentDraft,
    ShellDraft,
    open_terminal_behavior,
)
from relaydeck.client.features.inventory.selectors import workspace_shells
from relaydeck.client.features.sync.controller import SyncController
from relaydeck.client.features.sync.transport import (
    HttpxGatewayTransport,
    TransportError,
)

# design tokens (minimal first theme, centrally configurable).
from relaydeck.client.hooks.support import (
    SUBMIT_ERRORS,
    _log_creation_failure,
    new_command_id,
)
from relaydeck.domain.models import scope_key


def build_creation_actions(
    *,
    _revoked_now: Any,
    app_state: Any,
    app_state_box: Any,
    controller: SyncController,
    creation_drafts: Any,
    interaction_guard: Callable[[], bool] | None,
    launch_alive: Any,
    launch_busy: Any,
    launch_task: Any,
    navigate: Any,
    open_terminal: Any,
    pending_commands: Any,
    set_open_error: Any,
    set_sheet: Any,
    set_sheet_error: Any,
    set_sheet_status: Any,
    set_submitting: Any,
    sheet: Any,
    transport: HttpxGatewayTransport,
) -> tuple[Any, ...]:
    """Agent/shell creation: start, submit and cancel closures."""

    def start_creation(
        body: dict[str, Any],
        *,
        open_shell: bool = False,
        on_created: Callable[[str], None] | None = None,
    ) -> None:
        if interaction_guard is not None and not interaction_guard():
            return

        def allowed() -> bool:
            current = controller.state
            return not _revoked_now() and (
                current.granted_hosts is None or body["host_id"] in current.granted_hosts
            )

        if not allowed():
            set_sheet_error("Access revoked. Sign in again to continue.")
            return
        if launch_busy.current:
            return
        launch_busy.current = True

        async def run() -> None:
            try:
                if not allowed():
                    return
                set_submitting(True)
                set_sheet_error("")
                set_sheet_status("Sending…")
                record = await transport.submit_command(body)
                if record.get("command_id") != body["command_id"]:
                    raise CommandOutcomeUnknown("Creation identity mismatch; do not retry.")
                set_sheet_status("Waiting for the host outcome…")
                resource = await CreationObserver(
                    transport.command_status, transport.snapshot
                ).observe(body)
                if not allowed() or not launch_alive.current:
                    return
                if pending_commands is not None:
                    await pending_commands.settle_creation(
                        resource.host_id, body["command_id"], resource.resource_id
                    )
                creation_drafts.current.pop(
                    "start-agent" if resource.kind == "agent" else "new-terminal", None
                )
                if on_created is not None:
                    on_created(resource.key)
                if resource.snapshot is not None:
                    controller.accept_snapshot(resource.host_id, resource.snapshot)
                else:
                    controller.request_resync()
                    set_open_error("Created; host inventory refresh is pending.")
                set_sheet_status("Created.")
                set_sheet("")
                navigate("agents" if resource.kind == "agent" else "terminals", resource.key)
                if open_shell and resource.kind == "shell" and resource.snapshot is not None:
                    open_terminal("shell", resource.key)
            except TransportError as exc:
                if not exc.retryable and exc.code in (
                    "unauthenticated",
                    "forbidden",
                    "not_found",
                    "unsupported_capability",
                    "stale_epoch",
                    "stale_revision",
                    "command_conflict",
                ):
                    set_sheet_status("Request refused.")
                    set_sheet_error(SUBMIT_ERRORS.get(exc.code, exc.code))
                else:
                    _log_creation_failure(exc)
                    set_sheet_status("Outcome unknown.")
                    set_sheet_error(
                        "Creation outcome is unknown. Reconcile the command; do not retry."
                    )
            except CommandRefused as exc:
                set_sheet_status("Host refused creation.")
                partial = (exc.record or {}).get("result", {})
                if partial.get("type") == "partial_creation":
                    controller.request_resync()
                    set_sheet_error(
                        f"Creation stopped ({exc.code}); some resources were already created. "
                        "Refresh and choose the existing workspace before starting again."
                    )
                else:
                    set_sheet_error(f"Host refused creation ({exc.code}).")
            except Exception as exc:
                _log_creation_failure(exc)
                set_sheet_status("Outcome unknown.")
                set_sheet_error("Creation outcome is unknown. Reconcile the command; do not retry.")
            finally:
                launch_busy.current = False
                if launch_alive.current:
                    set_submitting(False)

        launch_task.current = asyncio.create_task(run())

    def workspace_spec(draft: AgentDraft | ShellDraft) -> dict[str, str] | None:
        if draft.workspace_mode != "new":
            return None
        return {
            "directory": draft.workspace_directory.strip(),
            "label": draft.workspace_label.strip(),
        }

    def submit_agent(draft: AgentDraft, argv: list[str]) -> None:
        current = app_state_box.current or app_state
        host = current.inventory.hosts.get(draft.host_id)
        start_creation(
            agent_start_command(
                command_id=new_command_id(),
                host_id=draft.host_id,
                host_epoch=host.epoch if host else "",
                workspace_id=draft.workspace_id,
                kind=draft.kind,
                name=draft.name,
                argv=argv,
                new_workspace=workspace_spec(draft),
                existing_names=frozenset(
                    agent.name
                    for agent in current.inventory.agents.values()
                    if agent.host_id == draft.host_id
                ),
            )
        )

    def submit_shell(draft: ShellDraft) -> None:
        current = app_state_box.current or app_state
        host = current.inventory.hosts.get(draft.host_id)
        start_creation(
            shell_create_command(
                command_id=new_command_id(),
                host_id=draft.host_id,
                host_epoch=host.epoch if host else "",
                workspace_id=draft.workspace_id,
                label=draft.label,
                new_workspace=workspace_spec(draft),
            )
        )

    def cancel_creation() -> None:
        if not launch_busy.current:
            creation_drafts.current.pop(sheet, None)
            set_sheet("")

    return (cancel_creation, start_creation, submit_agent, submit_shell)


def build_shell_origin_actions(
    *,
    _revoked_now: Any,
    controller: SyncController,
    launch_busy: Any,
    navigate: Any,
    open_terminal: Any,
    set_open_error: Any,
    set_sheet: Any,
    set_sheet_status: Any,
    shell_attempts: Any,
    shell_origin: Any,
    start_creation: Any,
) -> tuple[Any, ...]:
    """Shell choice from an agent's workspace: validation, choose, create, open."""

    def valid_shell_origin() -> Any:
        origin = shell_origin.current
        current = controller.state
        if origin is None or _revoked_now():
            return None
        key, host_id, workspace_id, epoch, revision = origin
        agent = current.inventory.agents.get(key)
        host = current.inventory.hosts.get(host_id)
        if (
            agent is None
            or agent.host_id != host_id
            or agent.workspace_id != workspace_id
            or agent.revision != revision
            or host is None
            or host.epoch != epoch
            or not host.reachable
            or scope_key(host_id, workspace_id) not in current.inventory.workspaces
            or current.selection.session_key != key
        ):
            set_sheet("")
            set_open_error("Agent context changed. Refresh before opening its shell.")
            return None
        return origin

    def choose_shell(key: str) -> None:
        origin = valid_shell_origin()
        if origin is None:
            return
        _, host_id, workspace_id, _, _ = origin
        choices = workspace_shells(controller.state, host_id, workspace_id)
        if key not in {scope_key(tab.host_id, tab.id) for tab in choices}:
            set_sheet("")
            set_open_error("That shell is no longer available in this workspace.")
            return
        set_sheet("")
        navigate("terminals", key)
        open_terminal("shell", key)

    def create_workspace_shell(*, fresh: bool = False) -> None:
        if launch_busy.current:
            return
        origin = valid_shell_origin()
        if origin is None:
            return
        _, host_id, workspace_id, epoch, _ = origin
        identity = (host_id, workspace_id, epoch)
        attempts = shell_attempts.current
        previous = attempts.get(identity)
        if previous is not None:
            created = previous["resource_key"]
            if created and created in controller.state.inventory.tabs:
                if not fresh:
                    choose_shell(created)
                    return
                del attempts[identity]  # Explicit New after proven visible creation.
            else:
                set_sheet("")
                set_open_error(
                    "A shell creation already exists for this workspace. Refresh and reconcile "
                    f"command {previous['body']['command_id']} before creating another."
                )
                controller.request_resync()
                return
        if len(attempts) >= 512:
            set_open_error(
                "Too many unresolved shell creations. Reconcile before creating another."
            )
            return
        set_sheet("shell-launch")
        set_sheet_status("")
        body = shell_create_command(
            command_id=new_command_id(),
            host_id=host_id,
            host_epoch=epoch,
            workspace_id=workspace_id,
        )
        attempt = {"body": body, "resource_key": ""}
        attempts[identity] = attempt
        start_creation(
            body, open_shell=True, on_created=lambda key: attempt.update(resource_key=key)
        )

    def open_shell_for_agent(key: str) -> None:
        current = controller.state
        agent = current.inventory.agents.get(key)
        if agent is None or _revoked_now():
            return
        host = current.inventory.hosts.get(agent.host_id)
        shell_origin.current = (
            key,
            agent.host_id,
            agent.workspace_id,
            host.epoch if host else "",
            agent.revision,
        )
        if valid_shell_origin() is None:
            return
        choices = workspace_shells(current, agent.host_id, agent.workspace_id)
        behavior = open_terminal_behavior(tuple(tab.id for tab in choices))
        if behavior == "create":
            create_workspace_shell()
        elif behavior == "open":
            choose_shell(scope_key(choices[0].host_id, choices[0].id))
        else:
            set_sheet("shell-chooser")

    return (choose_shell, create_workspace_shell, open_shell_for_agent)
