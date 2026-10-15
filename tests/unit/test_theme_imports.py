"""Startup dependency direction must work without a warmed module cache."""

import subprocess
import sys

import pytest


@pytest.mark.parametrize(
    "first",
    [
        "relaydeck.client.theme",
        "relaydeck.client.components.design_system",
    ],
)
def test_fresh_theme_application_import_and_public_exports(first: str) -> None:
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            f"""
import {first}
import relaydeck.client.theme as theme
import relaydeck.client.app
import relaydeck.client.components as components
from relaydeck.client.components.design_system import tokens
assert theme.MIN_TOUCH == tokens.MIN_TOUCH == 48
for name in components.__all__:
    assert getattr(components, name) is not None
assert components.HostContextHeader is components.HostContextHeader
try:
    components.nonexistent_component
except AttributeError:
    pass
else:
    raise AssertionError('Unknown export must fail')
""",
        ],
        capture_output=True,
        text=True,
        timeout=15,
        check=False,
    )
    assert result.returncode == 0, result.stderr
