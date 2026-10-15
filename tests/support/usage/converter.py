"""Shared price-converter test helpers (moved out of test modules)."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

SCRIPT = Path(__file__).parents[3] / "scripts/usage_prices_from_litellm.py"


def convert(tmp_path: Path, value: object, *extra: str) -> subprocess.CompletedProcess[str]:
    source = tmp_path / "input.json"
    source.write_text(json.dumps(value))
    return subprocess.run(
        [
            sys.executable,
            "-B",
            str(SCRIPT),
            str(source),
            "--snapshot-date",
            "2026-10-11",
            "--source-commit",
            "a" * 40,
            "--retention-days",
            "90",
            *extra,
        ],
        text=True,
        capture_output=True,
        timeout=5,
    )
