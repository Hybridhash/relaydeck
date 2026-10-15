"""Q5b: RendererBinding alone, driven through recording hooks (no session)."""

from __future__ import annotations

import asyncio
from typing import Any, cast

import pytest

from relaydeck.client.features.terminal.session.rendering import (
    CONTINUITY_NOTICE,
    DEFAULT_REOPEN_NOTICE,
    RendererBinding,
    RendererHooks,
)


class FakeTrace:
    def __init__(self, calls: list[str]) -> None:
        self.calls = calls

    def emit(self, event: str, **_fields: Any) -> None:
        self.calls.append(f"trace:{event}")


class FakeController:
    def __init__(self, calls: list[str]) -> None:
        self.calls = calls
        self.closed = asyncio.Event()

    def viewer_reopened(self) -> None:
        self.calls.append("controller.viewer_reopened")

    async def close(self) -> None:
        self.calls.append("controller.close")
        self.closed.set()

    def on_viewer_overflow(self) -> None:
        self.calls.append("overflow")


class FakeTerm:
    def __init__(self, generation: int = 0) -> None:
        self.viewer_generation = generation
        self.listeners: list[Any] = []
        self.fenced = 0
        self.resets: list[Any] = []

    def add_channel_listener(self, callback: Any) -> Any:
        self.listeners.append(callback)
        return lambda: self.listeners.remove(callback)

    def fence_fresh_viewer(self) -> None:
        self.fenced += 1

    def reset(self, **kw: Any) -> None:
        self.resets.append(kw)


class PlainTerm:
    """Renderer without viewer generations: reset takes no reason."""

    def __init__(self) -> None:
        self.resets = 0

    def reset(self) -> None:
        self.resets += 1


class Rig:
    def __init__(self, **overrides: Any) -> None:
        self.calls: list[str] = []
        self.controller: FakeController | None = FakeController(self.calls)
        self.foreground = True
        self.disposed = False
        self.exited = False
        self.sessions_bound = False
        self.stale: str | None = None
        self.disconnected: list[str] = []
        self.stale_notes: list[str | None] = []
        values: dict[str, Any] = {
            "session_id": 7,
            "controller": lambda: self.controller,
            "clear_controller": self._clear,
            "foreground": lambda: self.foreground,
            "disposed": lambda: self.disposed,
            "exited": lambda: self.exited,
            "sessions_bound": lambda: self.sessions_bound,
            "bind_sessions": lambda _c: self.calls.append("bind_sessions"),
            "invalidate_sessions": self._invalidate,
            "restart_renewal": lambda: self.calls.append("restart_renewal"),
            "cancel_renewal": lambda: self.calls.append("cancel_renewal"),
            "bump_interaction": lambda site: self.calls.append("bump_interaction"),
            "lease_trace": lambda: FakeTrace(self.calls),
            "notify": lambda: self.calls.append("notify"),
            "mark_disconnected": self._mark,
            "note_stale_notice": self._note,
        }
        values.update(overrides)
        self.binding = RendererBinding(RendererHooks(**values))

    def _clear(self) -> None:
        self.calls.append("clear_controller")
        self.controller = None

    def _invalidate(self) -> str | None:
        self.calls.append("invalidate")
        return self.stale

    def _mark(self, notice: str) -> None:
        self.calls.append("mark_disconnected")
        self.disconnected.append(notice)

    def _note(self, stale: str | None) -> None:
        self.calls.append("note_stale")
        self.stale_notes.append(stale)


def write_ok(_term: Any, _data: bytes) -> bool:
    return True


def test_sink_registration_tokens_and_liveness() -> None:
    binding = Rig().binding
    seen: list[bytes] = []
    first = binding.bind_renderer(seen.append)
    second = binding.bind_renderer(seen.append)
    assert (first, second) == (1, 2)
    assert binding.is_sink_live(first)
    binding.unbind_renderer(first)
    assert not binding.is_sink_live(first) and binding.is_sink_live(second)
    binding.deliver(b"x")
    assert seen == [b"x"]


def test_deliver_survives_a_dead_sink() -> None:
    binding = Rig().binding
    seen: list[bytes] = []

    def boom(_data: bytes) -> None:
        raise RuntimeError("renderer gone")

    binding.bind_renderer(boom)
    binding.bind_renderer(seen.append)
    binding.deliver(b"ok")
    assert seen == [b"ok"]


def test_bind_view_binds_unbound_sessions_and_restarts_renewal() -> None:
    rig = Rig()
    term = PlainTerm()
    token = rig.binding.bind_view(term, write=write_ok)
    assert rig.calls == ["bind_sessions", "restart_renewal", "notify"]
    assert rig.binding.view_token == token and rig.binding.current_term is term
    assert rig.binding.terms() == [term]


@pytest.mark.parametrize(
    "state",
    [{"sessions_bound": True}, {"foreground": False}, {"disposed": True}, {"controller": None}],
)
def test_bind_view_does_not_retarget_when_not_applicable(state: dict[str, Any]) -> None:
    rig = Rig()
    for name, value in state.items():
        setattr(rig, name, value)
    rig.binding.bind_view(PlainTerm(), write=write_ok)
    assert "bind_sessions" not in rig.calls


def test_bind_view_replaces_the_previous_mount() -> None:
    rig = Rig()
    first = rig.binding.bind_view(PlainTerm(), write=write_ok)
    second = rig.binding.bind_view(PlainTerm(), write=write_ok)
    assert not rig.binding.is_sink_live(first) and rig.binding.is_sink_live(second)
    assert len(rig.binding.terms()) == 1


def test_sink_writes_through_and_reports_overflow() -> None:
    rig = Rig()
    outcomes = iter([True, False])
    written: list[bytes] = []

    def write(_term: Any, data: bytes) -> bool:
        written.append(data)
        return next(outcomes)

    rig.binding.bind_view(PlainTerm(), write=write)
    rig.calls.clear()
    rig.binding.deliver(b"a")
    assert rig.calls == []
    rig.binding.deliver(b"b")
    assert written == [b"a", b"b"] and rig.calls == ["overflow", "notify"]


def test_stale_sink_after_unbind_is_ignored() -> None:
    rig = Rig()
    written: list[bytes] = []

    def write(_term: Any, data: bytes) -> bool:
        written.append(data)
        return True

    rig.binding.bind_view(PlainTerm(), write=write)
    rig.binding.unbind_view()
    rig.binding.deliver(b"late")
    assert written == []


def test_first_bind_of_a_native_viewer_does_not_report_a_reopen() -> None:
    rig = Rig()
    term = FakeTerm(generation=1)
    rig.binding.bind_view(term, write=write_ok)
    assert rig.binding.viewer_binding == (term, 1)
    assert term.fenced == 0 and "mark_disconnected" not in rig.calls
    assert "trace:viewer_bind" in rig.calls and len(term.listeners) == 1


def test_new_viewer_generation_fences_and_requires_reattach() -> None:
    async def go() -> None:
        rig = Rig()
        term = FakeTerm(generation=1)
        rig.binding.bind_view(term, write=write_ok)
        rig.calls.clear()
        term.viewer_generation = 2
        term.listeners[0]()
        await rig.binding.drain_close_tasks()
        assert term.fenced == 1
        assert rig.disconnected == [DEFAULT_REOPEN_NOTICE]
        assert rig.calls[0] == "trace:viewer_reopen"
        assert "controller.viewer_reopened" in rig.calls and rig.controller is None

    asyncio.run(go())


def test_unbind_removes_the_native_listener() -> None:
    rig = Rig()
    term = FakeTerm()
    rig.binding.bind_view(term, write=write_ok)
    assert len(term.listeners) == 1
    rig.binding.unbind_view()
    assert term.listeners == [] and rig.binding.current_term is None


def test_unbind_view_hook_order_and_stale_notice() -> None:
    rig = Rig()
    rig.stale = "stale draft"
    rig.binding.bind_view(PlainTerm(), write=write_ok)
    rig.calls.clear()
    rig.binding.unbind_view()
    assert rig.calls == ["bump_interaction", "cancel_renewal", "invalidate", "note_stale", "notify"]
    assert rig.stale_notes == ["stale draft"]


def test_optional_hooks_run_in_the_documented_order() -> None:
    calls: list[str] = []
    rig = Rig(
        cancel_watch=lambda: calls.append("cancel_watch"),
        bump_epoch=lambda: calls.append("bump_epoch"),
        detach_store=lambda: calls.append("detach_store"),
    )
    rig.binding.bind_view(PlainTerm(), write=write_ok)
    rig.calls.clear()
    rig.binding.unbind_view()
    assert calls == ["cancel_watch", "bump_epoch"]
    calls.clear()
    rig.controller = FakeController(rig.calls)

    async def go() -> None:
        rig.binding.viewer_reopened()
        await rig.binding.drain_close_tasks()

    asyncio.run(go())
    assert calls == ["cancel_watch", "bump_epoch", "detach_store"]


def test_viewer_reopened_sequence_and_close_task() -> None:
    rig = Rig()
    controller = cast(FakeController, rig.controller)

    async def go() -> None:
        rig.binding.viewer_reopened("custom notice")
        await rig.binding.drain_close_tasks()

    asyncio.run(go())
    assert rig.disconnected == ["custom notice"]
    assert rig.calls == [
        "mark_disconnected",
        "bump_interaction",
        "invalidate",
        "cancel_renewal",
        "controller.viewer_reopened",
        "clear_controller",
        "notify",
        "controller.close",
    ]
    assert controller.closed.is_set() and rig.controller is None


@pytest.mark.parametrize("guard", ["controller", "disposed", "exited"])
def test_viewer_reopened_is_a_no_op_when_guarded(guard: str) -> None:
    rig = Rig()
    if guard == "controller":
        rig.controller = None
    else:
        setattr(rig, guard, True)
    rig.binding.viewer_reopened()
    assert rig.calls == [] and rig.disconnected == []


def test_require_ui_recovery_uses_the_continuity_notice() -> None:
    rig = Rig()

    async def go() -> None:
        rig.binding.require_ui_recovery()
        await rig.binding.drain_close_tasks()

    asyncio.run(go())
    assert rig.disconnected == [CONTINUITY_NOTICE]


def test_reset_bound_renderers_passes_reason_only_to_native_viewers() -> None:
    rig = Rig()
    native, plain = FakeTerm(), PlainTerm()
    rig.binding.bind_view(native, write=write_ok)
    rig.binding.bind_renderer(lambda _d: None)
    rig.binding._terms[99] = plain
    rig.binding.reset_bound_renderers("stream_reset")
    assert native.resets == [{"reason": "stream_reset"}] and plain.resets == 1


def test_reset_bound_renderers_ignores_renderer_failures() -> None:
    rig = Rig()

    class Broken(FakeTerm):
        def reset(self, **kw: Any) -> None:
            raise RuntimeError("engine gone")

    rig.binding.bind_view(Broken(), write=write_ok)
    rig.binding.reset_bound_renderers()
