"""Golden: theme.toml must reproduce the look that predates it byte-for-byte.

The fixture was generated from the code before the theme file existed: every
value that existed then must still resolve identically; new names (loader
internals, unified roles) are allowed.
"""

from __future__ import annotations

import json
from pathlib import Path

from relaydeck.client import theme
from relaydeck.client.components.design_system import tokens as tokens_module

FIXTURE = Path(__file__).resolve().parents[2] / "fixtures" / "theme_golden.json"


def _public(module: object) -> dict[str, object]:
    out: dict[str, object] = {}
    for name in sorted(dir(module)):
        if name.startswith("__"):
            continue
        value = getattr(module, name)
        if callable(value) or isinstance(value, type):
            continue
        try:
            json.dumps(value)
        except (TypeError, ValueError):
            continue
        out[name] = value
    return out


def _current() -> dict[str, object]:
    return {
        "theme": _public(theme),
        "palette_dark": theme.palette(True),
        "palette_light": theme.palette(False),
        "tokens": _public(tokens_module),
        "usage_dark": tokens_module.usage_palette(True),
        "usage_light": tokens_module.usage_palette(False),
        "types": {
            name: getattr(tokens_module, name)
            for name in dir(tokens_module)
            if name.startswith("TYPE_")
        },
        "identities": {
            kind: [
                theme.agent_identity(kind, True).color,
                theme.agent_identity(kind, False).color,
                theme.agent_badge_ink(kind, False),
            ]
            for kind in ["alpha", "beta", "gamma", "delta", "shell", "", "zzz"]
        },
    }


def test_theme_values_match_pre_toml_golden() -> None:
    fixture = json.loads(FIXTURE.read_text())
    current = json.loads(json.dumps(_current()))
    for section, values in fixture.items():
        assert section in current
        if isinstance(values, dict):
            for key, expected in values.items():
                assert current[section][key] == expected, f"{section}.{key}"
        else:
            assert current[section] == values, section
