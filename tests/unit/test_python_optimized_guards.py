"""The fail-closed guards must hold with assertions stripped (`python -O`)."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
TESTS = (
    "tests/unit/terminal_session/test_keys.py"
    "::test_shell_typing_never_routes_to_a_draft_even_without_assert",
    "tests/unit/test_auth_boundary.py::test_callback_without_code_fails_closed_before_any_exchange",
    "tests/unit/test_usage_hook_install.py"
    "::test_upgrade_without_a_receipt_fails_closed_and_writes_nothing",
)


def test_guards_hold_under_python_dash_o() -> None:
    result = subprocess.run(
        [sys.executable, "-O", "-m", "pytest", "-q", "-p", "no:cacheprovider", *TESTS],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=300,
        check=False,
    )
    assert result.returncode == 0, result.stdout[-1500:]
    assert "3 passed" in result.stdout
