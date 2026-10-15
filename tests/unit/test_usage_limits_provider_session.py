"""Interleaved exchange, bounds, kill guarantees."""

from __future__ import annotations

import subprocess
import sys
import time

import pytest

from relaydeck.usage.contracts import UsageUnavailable
from relaydeck.usage.limits_plane.scheduler import (
    MAX_FETCH_BYTES,
    ExchangeStep,
    OutputCapExceeded,
    run_line_session,
)


def test_echo_interleaving() -> None:
    matched = run_line_session(
        ("/bin/cat",),
        (
            ExchangeStep(b"first", lambda line: line == b"first"),
            ExchangeStep(b"second", lambda line: line == b"second"),
            ExchangeStep(None, None),
        ),
        timeout_s=10,
    )
    assert matched == [b"first", b"second", None]


def test_non_matching_lines_dropped_until_match() -> None:
    script = (
        "import sys; sys.stdout.write('noise\\n'); sys.stdout.flush();"
        " sys.stdin.readline(); sys.stdout.write('ready\\n'); sys.stdout.flush();"
        " sys.stdin.read()"
    )
    matched = run_line_session(
        (sys.executable, "-c", script),
        (ExchangeStep(b"go", lambda line: line == b"ready"),),
        timeout_s=10,
    )
    assert matched == [b"ready"]


def test_send_only_step_returns_none() -> None:
    matched = run_line_session(("/bin/cat",), (ExchangeStep(b"hello", None),), timeout_s=5)
    assert matched == [None]


def test_rejects_bad_input() -> None:
    good = (ExchangeStep(b"x", lambda line: True),)
    with pytest.raises(ValueError):
        run_line_session("cat", good, timeout_s=5)  # type: ignore[arg-type]
    with pytest.raises(ValueError):
        run_line_session(("relative/cat",), good, timeout_s=5)
    with pytest.raises(ValueError):
        run_line_session(("/bin/cat",), "nope", timeout_s=5)  # type: ignore[arg-type]
    with pytest.raises(ValueError):
        run_line_session(("/bin/cat",), (), timeout_s=5)
    with pytest.raises(ValueError):
        run_line_session(("/bin/cat",), (("x", None),), timeout_s=5)  # type: ignore[list-item]
    with pytest.raises(ValueError):
        run_line_session(("/bin/cat",), good, timeout_s=0)
    with pytest.raises(UsageUnavailable):
        run_line_session(("/does/not/exist",), good, timeout_s=5)


def test_early_exit_maps_eof() -> None:
    with pytest.raises(EOFError):
        run_line_session(
            ("/usr/bin/true",),
            (ExchangeStep(None, lambda line: True),),
            timeout_s=5,
        )


def test_hang_times_out_and_kills_group() -> None:
    started = time.monotonic()
    with pytest.raises(TimeoutError):
        run_line_session(
            ("/bin/sleep", "32"),
            (ExchangeStep(None, lambda line: True),),
            timeout_s=0.2,
        )
    assert time.monotonic() - started < 10
    time.sleep(0.2)
    found = subprocess.run(["pgrep", "-f", "sleep 32"], capture_output=True, text=True)
    assert found.stdout == ""


def test_output_over_cap_kills_and_raises() -> None:
    script = "import sys,time; sys.stdout.write('z' * 2097152); sys.stdout.flush(); time.sleep(30)"
    started = time.monotonic()
    with pytest.raises(OutputCapExceeded):
        run_line_session(
            (sys.executable, "-c", script),
            (ExchangeStep(None, lambda line: False),),
            timeout_s=30,
        )
    assert time.monotonic() - started < 10
    assert MAX_FETCH_BYTES == 1024 * 1024


def test_minimal_env_for_sessions() -> None:
    seen: list[bytes] = []

    def watch(line: bytes) -> bool:
        seen.append(line)
        return False

    with pytest.raises(EOFError):  # env prints, exits; nothing matches
        run_line_session(
            ("/usr/bin/env",),
            (ExchangeStep(None, watch),),
            timeout_s=5,
            extra_env={"EXTRA_ENV_SESSION": "yes"},
        )
    keys = {line.split(b"=", 1)[0].decode() for line in seen if b"=" in line}
    assert "EXTRA_ENV_SESSION" in keys
    assert keys <= {"HOME", "PATH", "LANG", "EXTRA_ENV_SESSION"}
