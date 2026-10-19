"""The operator's SIGUSR1 prints Python frames without locals to foreground stderr."""

from __future__ import annotations

import subprocess
import sys


def test_owned_process_stack_dump_all_threads_and_handler_cleanup():
    script = """
import os
import signal
import threading
from relaydeck.companion.cli import thread_dumps

stop = threading.Event()
ready = threading.Event()
def diagnostic_waiter():
    bearer = "SYNTHETIC_BEARER_DO_NOT_PRINT"
    ready.set()
    stop.wait(3)

previous = signal.getsignal(signal.SIGUSR1)
worker = threading.Thread(target=diagnostic_waiter)
worker.start()
assert ready.wait(1)
try:
    with thread_dumps():
        cookie = "SYNTHETIC_COOKIE_DO_NOT_PRINT"
        os.kill(os.getpid(), signal.SIGUSR1)
    assert signal.getsignal(signal.SIGUSR1) == previous
finally:
    stop.set()
    worker.join(1)
print("diagnostic_complete")
"""
    result = subprocess.run(
        [sys.executable, "-B", "-c", script], capture_output=True, text=True, timeout=5
    )
    assert result.returncode == 0, result.stderr
    assert "diagnostic_complete" in result.stdout
    assert "diagnostic_waiter" in result.stderr and "Current thread" in result.stderr
    assert "SYNTHETIC_BEARER_DO_NOT_PRINT" not in result.stderr
    assert "SYNTHETIC_COOKIE_DO_NOT_PRINT" not in result.stderr
