"""Environment manifest check.

Prints pinned versions and fails when the running environment drifts from
the baseline: Flet 1.0.3, Python 3.13, Flutter 3.44.8 tooling.
"""

from __future__ import annotations

import importlib.metadata as md
import platform
import subprocess
import sys

EXPECTED_FLET = "1.0.3"
EXPECTED_PY_MINOR = (3, 13)
EXPECTED_FLUTTER = "3.44.8"


def main() -> int:
    failures: list[str] = []
    flet_version = md.version("flet")
    print(f"flet={flet_version}")
    if flet_version != EXPECTED_FLET:
        failures.append(f"flet {flet_version} != {EXPECTED_FLET}")
    for dist in ("flet-cli", "flet-web", "flet-desktop"):
        try:
            v = md.version(dist)
        except md.PackageNotFoundError:
            v = "not-installed"
        print(f"{dist}={v}")
        if v != EXPECTED_FLET:
            failures.append(f"{dist} {v} != {EXPECTED_FLET}")
    py = sys.version_info
    print(f"python={platform.python_version()}")
    if (py.major, py.minor) != EXPECTED_PY_MINOR:
        failures.append(f"python minor {(py.major, py.minor)} != {EXPECTED_PY_MINOR}")
    try:
        out = subprocess.run(
            ["flet", "--version"],  # noqa: S607 - checks the PATH tool, fixed argv
            capture_output=True,
            text=True,
            timeout=60,
        ).stdout
    except (OSError, subprocess.TimeoutExpired) as exc:
        out = ""
        failures.append(f"flet --version failed: {exc}")
    print(out.strip())
    if EXPECTED_FLUTTER not in out:
        failures.append(f"Flutter {EXPECTED_FLUTTER} not reported by flet --version")
    if failures:
        print("MISMATCH:")
        for failure in failures:
            print(f"  - {failure}")
        return 1
    print("environment OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
