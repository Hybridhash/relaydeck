"""Q5g: the session package keeps its documented shape."""

from __future__ import annotations

import importlib
import pkgutil
from pathlib import Path
from typing import Any

import pytest

import relaydeck.client.features.shell.session as shell_module
import relaydeck.client.features.terminal.agent_session as agent_module
import relaydeck.client.features.terminal.session as package
from relaydeck.client.features.shell.session import ShellSession
from relaydeck.client.features.terminal.agent_session import AgentSession
from relaydeck.client.features.terminal.session import hooks
from relaydeck.client.features.terminal.session.base import TerminalSessionBase
from relaydeck.client.features.terminal.session.facade import SessionFacade
from relaydeck.client.features.terminal.session.keys import KeyHooks
from relaydeck.client.features.terminal.session.lease import LeaseHooks
from relaydeck.client.features.terminal.session.paste import PasteHooks
from relaydeck.client.features.terminal.session.rendering import RendererHooks
from relaydeck.client.features.terminal.session.selection import SelectionHooks
from tests.support.scripted_sessions import make_scripted_sessions

MAX_SESSION_MODULE_LINES = 600  # a session module past this wants another split
MAX_SUBCLASS_LINES = 1000

# Every name the UI (surfaces, screen, retained_sessions, warm_watch, pictures,
# scrolling, ui_resume) and the tests reach through a session object.
PUBLIC_SURFACE = [
    "phase", "host_id", "picture_api", "sessions", "attachment", "lease", "controller",
    "term_state", "owner_label", "notice", "paused", "disposed", "foreground",
    "interaction_generation", "bracketed_paste", "current_term", "input_token",
    "renewal_running", "pending_review", "keys_enabled", "paste_error", "armed_modifiers",
    "selection_sheet_text", "subscribe", "set_foreground", "set_bracketed_paste",
    "update_term_state", "bind_renderer", "unbind_renderer", "is_sink_live", "bind_view",
    "unbind_view", "require_ui_recovery", "start_lease_renewal", "stop_lease_renewal",
    "type_text", "handle_view_input", "press_key", "toggle_modifier", "interrupt",
    "flush_keys", "request_paste", "confirm_paste", "cancel_paste", "paste_from_clipboard",
    "stale_review_notice", "open_selection", "open_selection_snapshot", "move_selection",
    "close_selection", "jump_to_bottom", "copy_selection", "resize", "run", "disconnect",
    "reconnect", "pause", "resume_after_pause", "suspend_input", "detach", "dispose",
    "drive_recovery", "view",
    # private names the UI/tests rely on
    "_renewal", "_view_token", "_viewer_binding", "_selection_snapshot", "_token_live",
    "_viewer_reopened", "_reset_bound_renderers", "_restart_renewal", "_lease_trace",
    "_release_stale_lease", "_on_renewal_terminal", "_operation_current", "_notify",
    "_classify_run_exit", "_spawn_controller",
]  # fmt: skip


def line_count(module: Any) -> int:
    return len(Path(module.__file__).read_text().splitlines())


def test_package_docstring_describes_the_design() -> None:
    doc = package.__doc__ or ""
    for term in (
        "TerminalSessionBase",
        "RendererBinding",
        "LeaseRenewalOwner",
        "SelectionController",
        "PasteFlow",
        "KeyInput",
        "hooks",
        "Agent Composer",
        "Agent Direct",
        "Shell",
    ):
        assert term in doc


def test_every_session_module_has_a_docstring_and_stays_small() -> None:
    names = [m.name for m in pkgutil.iter_modules(package.__path__)]
    assert {"base", "facade", "hooks", "keys", "lease", "paste", "rendering", "selection"} <= set(
        names
    )
    for name in names:
        module = importlib.import_module(f"{package.__name__}.{name}")
        assert module.__doc__, name
        assert line_count(module) <= MAX_SESSION_MODULE_LINES, name


def test_the_two_subclasses_stay_small_enough_to_review() -> None:
    assert line_count(agent_module) <= MAX_SUBCLASS_LINES
    assert line_count(shell_module) <= MAX_SUBCLASS_LINES


@pytest.mark.parametrize("session_type", [AgentSession, ShellSession])
@pytest.mark.parametrize("name", PUBLIC_SURFACE)
def test_the_session_surface_is_unchanged(session_type: type, name: str) -> None:
    assert hasattr(session_type, name), name


def test_base_composes_the_facade_and_declares_only_real_differences_as_abstract() -> None:
    assert issubclass(TerminalSessionBase, SessionFacade)
    assert TerminalSessionBase.__abstractmethods__ == {
        "_on_attach_lost",
        "_workflow_kind",
        "_reconnect_flow",
        "_terminal_label",
    }


def test_identical_bodies_live_once_in_the_base() -> None:
    for name in (
        "_spawn_controller",
        "_classify_run_exit",
        "drive_recovery",
        "_on_renewal_terminal",
    ):
        assert name in vars(TerminalSessionBase)
        assert name not in vars(AgentSession) and name not in vars(ShellSession), name


def test_hook_builders_return_the_collaborator_hooks_for_a_session() -> None:
    scripted = make_scripted_sessions()
    agent = scripted.factory.make_agent(host_id="mac", host_epoch="h1", agent_id="a", target="a")
    shell = scripted.factory.make_shell(host_id="mac")
    for session in (agent, shell):
        assert isinstance(hooks.renderer_hooks(session), RendererHooks)
        assert isinstance(hooks.lease_hooks(session), LeaseHooks)
        assert isinstance(hooks.selection_hooks(session), SelectionHooks)
        assert isinstance(hooks.paste_hooks(session), PasteHooks)
        assert isinstance(hooks.key_hooks(session), KeyHooks)
        assert isinstance(session._renderer_hooks(), RendererHooks)
    assert hooks.selection_hooks(agent).owner == "AgentSession"
    assert hooks.selection_hooks(shell).owner == "ShellSession"
    assert hooks.key_hooks(agent).allow_draft and not hooks.key_hooks(shell).allow_draft
