"""Only a real return or a reconnect touches geometry; focus bounces never do."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

from relaydeck.client.resources import PROBE_AFTER_ABSENCE, AppResources
from relaydeck.client.retained_sessions import RetainedSessions


class Size:
    def __init__(self) -> None:
        self.resets = 0

    def reset_baselines(self) -> None:
        self.resets += 1


def _resources() -> tuple[AppResources, Size, Any, list[float]]:
    size, now = Size(), [1000.0]
    session = SimpleNamespace(notes=0)
    session.note_activation = lambda: setattr(session, "notes", session.notes + 1)
    resources = AppResources(
        props={},
        client=None,  # type: ignore[arg-type]
        owns_client=False,
        retained=RetainedSessions(),
        bridge=None,  # type: ignore[arg-type]
        controller=None,  # type: ignore[arg-type]
        transport=None,  # type: ignore[arg-type]
        commands=None,  # type: ignore[arg-type]
        visible_size=size,  # type: ignore[arg-type]
        _clock=lambda: now[0],
    )
    resources.retained.foreground = SimpleNamespace(session=session)  # type: ignore[assignment]
    resources.recovery = SimpleNamespace(request=lambda entry, reason: None)  # type: ignore[assignment]
    resources._reconcile_terminal = lambda entry, force=False: None  # type: ignore[method-assign]
    return resources, size, session, now


def test_twenty_focus_bounces_leave_geometry_alone() -> None:
    resources, size, session, now = _resources()
    for _ in range(20):
        resources.activate("show")
        now[0] += 0.1
        resources.activate("resume")
        now[0] += 0.1
    assert size.resets == 0 and session.notes == 0


def test_a_real_return_resets_once_and_rearms_once() -> None:
    resources, size, session, now = _resources()
    resources.note_hidden()
    now[0] += PROBE_AFTER_ABSENCE + 1
    resources.activate("show")
    resources.activate("resume")  # the same return's second event
    assert size.resets == 1 and session.notes == 1


def test_a_session_reconnect_counts_as_a_return() -> None:
    resources, size, session, _now = _resources()
    resources.activate("connect", probe=False)
    assert size.resets == 1 and session.notes == 1
