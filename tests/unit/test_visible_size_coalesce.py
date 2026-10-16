"""The visible size publishes once per keyboard change, with the final values."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

from relaydeck.client.features.inventory.routing import RouteBridge
from relaydeck.client.resources import VisibleSize

WIDTH, HEIGHT = 390.0, 844.0


class Page:
    def __init__(self) -> None:
        self.width, self.height = WIDTH, HEIGHT
        self.media = SimpleNamespace(view_insets=SimpleNamespace(bottom=0.0))
        self.on_resize: Any = None
        self.on_media_change: Any = None

    def inset(self, bottom: float) -> None:
        self.media.view_insets.bottom = bottom
        self.on_media_change(None)

    def resize(self, height: float) -> None:
        self.height = height
        self.on_resize(None)


class Timeline:
    """A synthetic clock whose scheduled callbacks run when time reaches them."""

    def __init__(self) -> None:
        self.now = 100.0
        self.due: list[list[Any]] = []

    def schedule(self, delay: float, callback: Any) -> Any:
        entry = [self.now + delay, callback, False]
        self.due.append(entry)
        return SimpleNamespace(cancel=lambda: entry.__setitem__(2, True))

    def advance(self, seconds: float) -> None:
        end = self.now + seconds
        while True:
            ready = sorted((e for e in self.due if not e[2] and e[0] <= end), key=lambda e: e[0])
            if not ready:
                break
            entry = ready[0]
            entry[2] = True
            self.now = max(self.now, entry[0])
            entry[1]()
        self.now = end


def _owned() -> tuple[Page, RouteBridge, VisibleSize, Timeline, list[tuple[float, float]]]:
    page, timeline = Page(), Timeline()
    bridge = RouteBridge(width=WIDTH, height=HEIGHT)
    published: list[tuple[float, float]] = []
    bridge.subscribe(lambda state: published.append((state.width, state.height)))
    owner = VisibleSize(page, bridge, clock=lambda: timeline.now, schedule=timeline.schedule)
    owner.install()
    return page, bridge, owner, timeline, published


def test_the_keyboard_and_its_accessory_bar_publish_once() -> None:
    page, _bridge, _owner, timeline, published = _owned()
    timeline.advance(1.0)
    page.inset(300)
    timeline.advance(0.03)
    page.inset(340)
    assert published == []  # nothing is published mid-burst
    timeline.advance(0.2)
    assert published == [(WIDTH, HEIGHT - 340)]


def test_a_dismissal_goes_straight_to_the_full_height() -> None:
    page, _bridge, _owner, timeline, published = _owned()
    timeline.advance(1.0)
    page.inset(340)
    timeline.advance(0.2)
    published.clear()
    page.inset(0)
    timeline.advance(0.5)
    assert published == [(WIDTH, HEIGHT)]


def test_an_input_view_swap_never_publishes_the_empty_state() -> None:
    page, _bridge, _owner, timeline, published = _owned()
    timeline.advance(1.0)
    page.inset(340)
    timeline.advance(0.2)
    published.clear()
    page.inset(0)
    timeline.advance(0.1)
    page.inset(340)
    timeline.advance(1.0)
    assert all(height != HEIGHT for _width, height in published)


def test_the_first_measurement_is_not_delayed() -> None:
    page, bridge, owner, timeline, published = _owned()
    # Install measured immediately: no advance of time was needed.
    assert bridge.state.height == HEIGHT
    page.height = 700.0
    owner.reset_baselines()
    page.on_resize(None)
    assert bridge.state.height == 700.0  # undelayed after a reset
    assert timeline.due == [] or all(entry[2] for entry in timeline.due)
    assert published[-1] == (WIDTH, 700.0)


def test_a_reset_drops_the_baseline_but_keeps_an_active_keyboard_one() -> None:
    page, bridge, owner, timeline, _published = _owned()
    timeline.advance(1.0)
    page.inset(340)
    timeline.advance(0.2)
    owner.reset_baselines()
    page.resize(HEIGHT - 340)  # the browser shrank the page for the keyboard
    timeline.advance(0.2)
    assert bridge.state.height == HEIGHT - 340  # not subtracted twice


def _dismiss_with_a_late_viewport(restore_before_reopen: bool) -> tuple[float, float]:
    page, bridge, _owner, timeline, _published = _owned()
    timeline.advance(1.0)
    page.inset(340)
    timeline.advance(0.2)
    first_open = bridge.state.height
    page.inset(0)  # dismissed, but the viewport has not grown back yet
    page.resize(HEIGHT - 340)
    timeline.advance(0.5)
    if restore_before_reopen:
        page.resize(HEIGHT)
    timeline.advance(1.0 - (0.5 if restore_before_reopen else 0.0))
    page.inset(340)
    timeline.advance(0.3)
    return first_open, bridge.state.height


def test_a_late_viewport_after_dismissal_does_not_double_subtract_the_next_open() -> None:
    first, reopened = _dismiss_with_a_late_viewport(restore_before_reopen=False)
    assert first == reopened == HEIGHT - 340


def test_a_restored_viewport_gives_the_same_rows_on_reopen() -> None:
    first, reopened = _dismiss_with_a_late_viewport(restore_before_reopen=True)
    assert first == reopened == HEIGHT - 340
