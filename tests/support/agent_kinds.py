"""synthetic agent kinds on a temporary PATH; no real agent is started."""

from __future__ import annotations

from pathlib import Path

import pytest


def allow_kinds(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, *names: str) -> tuple[str, ...]:
    """Put a no-op executable for each synthetic kind on PATH (the connector's PATH check)."""
    bin_dir = tmp_path / "synthetic-bin"
    bin_dir.mkdir(exist_ok=True)
    for name in names:
        path = bin_dir / name
        path.write_text("#!/bin/sh\nexit 0\n")
        path.chmod(0o700)
    monkeypatch.setenv("PATH", str(bin_dir))
    return names
