"""Terminals attach at the page's measured size, not a guessed 80x24."""

from types import SimpleNamespace

import pytest

from relaydeck.client.features.terminal.geometry import (
    DEFAULT_GEOMETRY,
    page_geometry,
    reported_geometry,
)
from tests.support.host_harness import run
from tests.support.terminal.reader_handoff import _actor
from tests.support.terminal.switch_gap import _Renderer, _write


def _term(size: object) -> SimpleNamespace:
    return SimpleNamespace(_last_view_size=size)


@pytest.mark.parametrize(
    ("size", "expected"),
    [
        ((59, 37), (59, 37)),
        ((0, 0), None),  # nothing applied yet
        ((59,), None),
        ((59.0, 37), None),
        ((2000, 37), None),
        (None, None),
    ],
)
def test_only_an_applied_in_bounds_size_counts(size: object, expected: object) -> None:
    assert reported_geometry(_term(size)) == expected


def test_the_first_valid_terminal_wins_and_none_falls_back() -> None:
    assert page_geometry([None, _term((0, 0)), _term((59, 37)), _term((80, 24))]) == (59, 37)
    assert page_geometry([None, _term((0, 0))]) is None
    assert DEFAULT_GEOMETRY == (80, 24)


def test_a_takeover_attaches_at_the_bound_viewers_measured_size() -> None:
    async def scenario() -> None:
        async with _actor("agent") as ctx:
            actor = ctx.actor
            sizes: list[tuple[int, int]] = []
            real_attach = actor._ops.attach

            async def attach(host, target, kind, takeover, cols=80, rows=24):  # type: ignore[no-untyped-def]
                sizes.append((cols, rows))
                return await real_attach(host, target, kind, takeover, cols, rows)

            actor._ops.attach = attach
            term = _Renderer()
            term._last_view_size = (59, 37)
            actor.bind_view(term, write=_write)
            actor.request_takeover()
            await actor.confirm_takeover()
            assert sizes == [(59, 37)]

    run(scenario())
