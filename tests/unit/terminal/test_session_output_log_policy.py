"""Bounded session output log policy.

`BoundedOutputLog` keeps the newest bytes under an explicit cap and
reports how much was dropped. The full-session enforcement proofs live
in tests/integration/terminal/test_session_paths.py (a ring-only test is
insufficient).
"""

from __future__ import annotations

from relaydeck.client.features.terminal.output_log import (
    OUTPUT_LOG_LIMIT,
    BoundedOutputLog,
)


def test_log_appends_and_reports_bytes() -> None:
    log = BoundedOutputLog(limit=16)
    log += b"hello "
    log += b"world"
    assert bytes(log) == b"hello world"
    assert len(log) == 11
    assert log.dropped_bytes == 0


def test_log_evicts_oldest_over_the_cap() -> None:
    log = BoundedOutputLog(limit=8)
    log += b"0123456789ABCDEF"
    assert bytes(log) == b"89ABCDEF"
    assert len(log) == 8
    assert log.dropped_bytes == 8


def test_log_single_write_larger_than_cap_keeps_tail() -> None:
    log = BoundedOutputLog(limit=4)
    log += b"0123456789"
    assert bytes(log) == b"6789"
    assert log.dropped_bytes == 6


def test_log_clear_resets_without_forgetting_drops() -> None:
    log = BoundedOutputLog(limit=4)
    log += b"0123456789"
    log.clear()
    assert bytes(log) == b""
    assert len(log) == 0
    assert log.dropped_bytes == 6


def test_log_default_limit_is_explicit_and_bounded() -> None:
    assert OUTPUT_LOG_LIMIT == 256 * 1024
    log = BoundedOutputLog()
    assert log.limit == OUTPUT_LOG_LIMIT
    log += b"x" * (OUTPUT_LOG_LIMIT + 1)
    assert len(log) == OUTPUT_LOG_LIMIT
