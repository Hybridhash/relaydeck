"""Q5f: TerminalSessionBase skeleton, template order and abstract hooks."""

from __future__ import annotations

import asyncio
import inspect
from typing import Any, cast

import pytest

from relaydeck.client.features.shell.ops import LeaseInfo, ShellOpError
from relaydeck.client.features.shell.session import ShellSession
from relaydeck.client.features.sync.transport import TransportError
from relaydeck.client.features.terminal.agent_session import AgentSession
from relaydeck.client.features.terminal.controller import TerminalController
from relaydeck.client.features.terminal.input_modes import (
    WORKFLOW_AGENT_COMPOSER,
    WORKFLOW_AGENT_DIRECT,
    WORKFLOW_SHELL,
    InputModeState,
)
from relaydeck.client.features.terminal.renewal import utc_stamp_in
from relaydeck.client.features.terminal.session.base import TerminalSessionBase
from relaydeck.client.features.terminal.surfaces import DetailSessions
from tests.support.scripted_sessions import make_scripted_sessions

# Q5g merged `_spawn_controller`, `_classify_run_exit`, `drive_recovery` and the shared
# core of `_on_renewal_terminal` into the base: only genuine differences stay abstract.
ABSTRACT = {"_on_attach_lost", "_workflow_kind", "_reconnect_flow", "_terminal_label"}


def make_lease(lease_id: str = "L1") -> LeaseInfo:
    return LeaseInfo(
        lease_id=lease_id,
        attachment_id="att",
        host_id="mac",
        terminal_id="t",
        mode="control",
        generation=1,
        epoch="e1",
        expires_at=utc_stamp_in(60),
    )


class FakeOps:
    picture_api = None

    def __init__(self, calls: list[str]) -> None:
        self.calls = calls
        self.error: Exception | None = None

    async def release_lease(self, lease_id: str) -> None:
        self.calls.append(f"release:{lease_id}")
        if self.error is not None:
            raise self.error


class FakeController:
    def __init__(self, calls: list[str], outcome: Exception | None = None) -> None:
        self.calls = calls
        self.outcome = outcome

    def disconnected(self) -> None:
        self.calls.append("controller.disconnected")

    def pause_writer(self) -> None:
        self.calls.append("controller.pause_writer")

    async def close(self) -> None:
        self.calls.append("controller.close")

    async def run(self) -> None:
        self.calls.append("controller.run")
        if self.outcome is not None:
            raise self.outcome


class Stub(TerminalSessionBase):
    """Minimal subclass recording every hook the base templates call."""

    _PHASE_LIVE = "live"
    _PHASE_EXITED = "exited"
    _PHASE_DISCONNECTED = "disconnected"
    _PHASE_CONFLICT = "conflict"
    _DISCONNECT_MESSAGE = "disconnected msg"
    _PAUSE_MESSAGE = "paused msg"
    _NOUN = "stub"

    def __init__(self) -> None:
        self.calls: list[str] = []
        self.ops = FakeOps(self.calls)
        super().__init__(
            ops=cast(Any, self.ops),
            host_id="mac",
            device_id="dev",
            socket_factory=lambda _t: cast(Any, None),
            clipboard=None,
            on_bytes=None,
            sessions=DetailSessions(modes=InputModeState(workflow=WORKFLOW_SHELL)),
            phase="connecting",
            renew_lease=None,
        )
        self.continue_runs = 0

    # required hooks
    def _spawn_controller(self, lease: LeaseInfo) -> TerminalController:
        raise AssertionError("not used")

    def _classify_run_exit(self) -> str:
        self.calls.append("classify")
        self._phase = "disconnected"
        return self._phase

    def _on_attach_lost(self) -> None:
        self.calls.append("attach_lost")

    @property
    def _workflow_kind(self) -> str:
        return WORKFLOW_SHELL

    def _on_renewal_terminal(self, code: str) -> None:
        self.calls.append(f"terminal:{code}")

    async def _reconnect_flow(self) -> str:
        self.calls.append("reconnect_flow")
        return self._phase

    def _terminal_label(self) -> str:
        return "t"

    # optional hooks, recorded
    def _cancel_background_recovery(self) -> None:
        self.calls.append("cancel_bg")

    def _bump_epoch(self) -> None:
        self.calls.append("bump_epoch")

    def _clear_composer(self) -> None:
        self.calls.append("clear_composer")

    async def _before_pause_close(self) -> None:
        self.calls.append("before_pause_close")

    async def _after_resume_reconnect(self) -> None:
        self.calls.append("after_resume")

    async def _continue_after_run(
        self, controller: Any, snapshot: Any, failure: Any, context: Any
    ) -> bool:
        self.calls.append("continue")
        self.continue_runs += 1
        return self.continue_runs < 2


def run(coro: Any) -> Any:
    return asyncio.run(coro)


def test_both_sessions_inherit_the_skeleton_and_the_base_is_abstract() -> None:
    assert issubclass(AgentSession, TerminalSessionBase)
    assert issubclass(ShellSession, TerminalSessionBase)
    assert TerminalSessionBase.__abstractmethods__ == ABSTRACT
    assert not inspect.isabstract(AgentSession) and not inspect.isabstract(ShellSession)
    with pytest.raises(TypeError, match="abstract"):
        TerminalSessionBase()  # type: ignore[abstract,call-arg]


def test_a_subclass_missing_the_hooks_cannot_be_built() -> None:
    class Missing(TerminalSessionBase):
        pass

    with pytest.raises(TypeError, match="abstract"):
        Missing()  # type: ignore[abstract,call-arg]


def test_three_workflows_stay_distinct() -> None:
    scripted = make_scripted_sessions()
    agent = scripted.factory.make_agent(host_id="mac", host_epoch="h1", agent_id="a", target="a")
    shell = scripted.factory.make_shell(host_id="mac")
    assert agent._workflow_kind == WORKFLOW_AGENT_COMPOSER
    agent.set_workflow(WORKFLOW_AGENT_DIRECT)
    assert agent._workflow_kind == WORKFLOW_AGENT_DIRECT
    assert shell._workflow_kind == WORKFLOW_SHELL
    # The Composer is agent-only: the shell has no store and may never route to a draft.
    assert shell._composer_store() is None and agent._composer_store() is not None
    assert agent._key_input._hooks.allow_draft and not shell._key_input._hooks.allow_draft
    assert agent._terminal_label() == agent.target and shell._terminal_label() == shell.terminal_id


def test_collaborators_are_composed_not_inherited() -> None:
    session = Stub()
    for name in ("_renderer", "_lease_owner", "_selection_ctl", "_paste_flow", "_key_input"):
        assert name in vars(session)
    assert session.phase == "connecting" and session.host_id == "mac"
    assert session.sessions is session._sessions


def test_subscribe_notifies_until_unsubscribed() -> None:
    session = Stub()
    seen: list[Any] = []
    unsubscribe = session.subscribe(seen.append)
    session._notify()
    unsubscribe()
    unsubscribe()  # idempotent
    session._notify()
    assert seen == [session]


def test_disconnect_template_order_and_lease_release() -> None:
    async def go() -> None:
        s = Stub()
        s._lease = make_lease()
        s._controller = cast(Any, FakeController(s.calls))
        gen = s.interaction_generation
        assert await s.disconnect() == "disconnected"
        assert s.calls == [
            "cancel_bg",
            "controller.disconnected",
            "attach_lost",
            "controller.close",
            "release:L1",
        ]
        assert s.interaction_generation == gen + 1
        assert s.notice == "disconnected msg" and s.lease is None

    run(go())


def test_disconnect_release_failure_is_disclosed_with_the_noun_and_never_retried() -> None:
    async def go() -> None:
        s = Stub()
        s._lease = make_lease()
        s.ops.error = ShellOpError("gone", "release failed")
        await s.disconnect()
        assert s.notice == "Detach noted (gone); the stub keeps running."
        assert s.calls.count("release:L1") == 1

    run(go())


def test_disconnect_never_leaves_exited() -> None:
    async def go() -> None:
        s = Stub()
        s._phase = "exited"
        assert await s.disconnect() == "exited"

    run(go())


def test_suspend_input_fences_once() -> None:
    s = Stub()
    gen = s.interaction_generation
    s.suspend_input()
    assert s.paused and s.interaction_generation == gen + 1
    assert s.calls == ["cancel_bg", "attach_lost"]
    s.suspend_input()
    assert s.calls == ["cancel_bg", "attach_lost"] and s.interaction_generation == gen + 1


def test_pause_template_order_and_notice() -> None:
    async def go() -> None:
        s = Stub()
        s._lease = make_lease()
        assert await s.pause() == "disconnected"
        assert s.calls == [
            "cancel_bg",
            "attach_lost",
            "before_pause_close",
            "release:L1",
        ]
        assert s.paused and s.notice == "paused msg"

    run(go())


def test_pause_is_a_no_op_once_exited() -> None:
    async def go() -> None:
        s = Stub()
        s._phase = "exited"
        assert await s.pause() == "exited"
        assert s.calls == [] and not s.paused

    run(go())


def test_reconnect_gate_cancels_recovery_then_refuses_when_exited() -> None:
    async def go() -> None:
        s = Stub()
        s._phase = "exited"
        assert await s.reconnect() == "exited"
        assert s.calls == ["cancel_bg"]
        s._phase = "disconnected"
        s.calls.clear()
        await s.reconnect()
        assert s.calls == ["cancel_bg", "reconnect_flow"]

    run(go())


def test_resume_after_pause_clears_the_marker_and_runs_the_resume_hook() -> None:
    async def go() -> None:
        s = Stub()
        s._paused = True
        s._phase = "disconnected"
        await s.resume_after_pause()
        assert not s.paused and s.calls == ["cancel_bg", "reconnect_flow", "after_resume"]

    run(go())


def test_set_foreground_bumps_the_generation_and_fences_hidden_input() -> None:
    s = Stub()
    gen = s.interaction_generation
    s.set_foreground(True)  # unchanged: nothing happens
    assert s.calls == [] and s.interaction_generation == gen
    s.set_foreground(False)
    assert not s.foreground and s.interaction_generation == gen + 1
    assert s.calls == ["cancel_bg", "bump_epoch"]


def test_dispose_order_and_sensitive_clear() -> None:
    async def go() -> None:
        s = Stub()
        s._notice = "owner name"
        s.output_log += b"secret"
        await s.dispose(clear_sensitive=True)
        assert s.disposed and s.calls[0] == "cancel_bg"
        assert s.calls.index("attach_lost") < s.calls.index("clear_composer")
        assert s.notice == "" and bytes(s.output_log) == b""

        keep = Stub()
        keep._notice = "kept"
        await keep.dispose()
        assert keep.disposed and "clear_composer" not in keep.calls

    run(go())


def test_run_without_a_controller_returns_the_phase() -> None:
    async def go() -> None:
        s = Stub()
        assert await s.run() == "connecting" and s.calls == []

    run(go())


def test_run_classifies_a_clean_end_and_asks_the_hook_whether_to_continue() -> None:
    async def go() -> None:
        s = Stub()
        s._controller = cast(Any, FakeController(s.calls))
        assert await s.run() == "disconnected"
        assert s.calls == [
            "controller.run",
            "classify",
            "continue",
            "controller.run",
            "classify",
            "continue",
        ]

    run(go())


def test_run_marks_a_transport_error_disconnected_with_the_message() -> None:
    async def go() -> None:
        s = Stub()
        s._controller = cast(Any, FakeController(s.calls, TransportError("link", "drop", True)))
        s._phase = "live"
        s.continue_runs = 5  # the hook says stop
        assert await s.run() == "disconnected"
        assert s.notice == "disconnected msg" and "classify" not in s.calls

    run(go())


def test_run_leaves_an_exited_phase_to_the_classifier_after_a_transport_error() -> None:
    async def go() -> None:
        s = Stub()
        s._controller = cast(Any, FakeController(s.calls, TransportError("link", "drop", True)))
        s._phase = "exited"
        s.continue_runs = 5
        await s.run()
        # The base never overwrites an exited phase with the disconnect message.
        assert s.notice == "" and "classify" in s.calls

    run(go())


def test_run_returns_early_when_the_controller_was_replaced_during_the_stream() -> None:
    async def go() -> None:
        s = Stub()
        controller = FakeController(s.calls)

        async def swap() -> None:
            s.calls.append("controller.run")
            s._controller = cast(Any, FakeController(s.calls))

        controller.run = swap  # type: ignore[method-assign]
        s._controller = cast(Any, controller)
        assert await s.run() == "connecting"
        assert "classify" not in s.calls and "continue" not in s.calls

    run(go())
