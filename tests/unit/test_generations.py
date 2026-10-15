"""writer generation tracking."""

from __future__ import annotations

from relaydeck.domain.generations import GenerationTracker


def test_begin_and_current() -> None:
    tracker = GenerationTracker()
    tracker, gen = tracker.begin("term-1")
    assert gen == 1
    assert tracker.is_current("term-1", 1) is True
    assert tracker.is_current("term-1", 0) is False


def test_replace_invalidates_predecessor() -> None:
    tracker = GenerationTracker()
    tracker, first = tracker.begin("term-1")
    tracker, second = tracker.begin("term-1")
    assert second == first + 1
    assert tracker.is_current("term-1", first) is False
    assert tracker.is_current("term-1", second) is True


def test_invalidate_without_replacement() -> None:
    tracker = GenerationTracker()
    tracker, gen = tracker.begin("term-1")
    tracker = tracker.invalidate("term-1")
    assert tracker.is_current("term-1", gen) is False


def test_terminals_independent() -> None:
    tracker = GenerationTracker()
    tracker, _ = tracker.begin("term-1")
    tracker, gen2 = tracker.begin("term-2")
    assert gen2 == 1
    tracker = tracker.invalidate("term-1")
    assert tracker.is_current("term-2", gen2) is True


def test_tracker_immutable() -> None:
    tracker = GenerationTracker()
    tracker2, _ = tracker.begin("term-1")
    assert tracker.current == {}
    assert tracker2.current == {"term-1": 1}
