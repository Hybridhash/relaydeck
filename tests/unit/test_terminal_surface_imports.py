"""Terminal surfaces load first without warming the application's import graph."""

import subprocess
import sys

import pytest


@pytest.mark.auto_takeover
@pytest.mark.open_hold
def test_fresh_surface_import_without_application_first() -> None:
    # Skip UI-default fixtures: this test owns an otherwise unwarmed interpreter.
    result = subprocess.run(
        [
            sys.executable,
            "-B",
            "-c",
            """
import sys
assert 'relaydeck.client.app' not in sys.modules
from relaydeck.client.features.terminal.surfaces import DRIVE_REFUSED_EXITED, drive_recovery
assert DRIVE_REFUSED_EXITED == 'refused-exited'
assert callable(drive_recovery)
assert 'relaydeck.client.app' not in sys.modules
""",
        ],
        capture_output=True,
        text=True,
        timeout=15,
        check=False,
    )
    assert result.returncode == 0, result.stderr
