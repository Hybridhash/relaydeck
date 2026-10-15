#!/usr/bin/env bash
# Proves that the built RelayDeck wheel installs and runs outside this checkout.
#
#   1. builds the wheel and sdist into a temporary directory (nothing is written under dist/ here);
#   2. creates a temporary virtualenv outside the repository;
#   3. installs the built wheel offline, from the uv cache only (no network);
#   4. runs the installed commands (help output and `setup --check`) with a temporary HOME, so no real configuration is read or written;
#   5. imports every module of every distribution package;
#   6. asserts that the package data the runtime reads is present;
#   7. removes the temporary directory.
#
# Nothing is published. Exit status is non-zero if any step fails.
set -uo pipefail

repo="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
work="$(mktemp -d "${TMPDIR:-/tmp}/relaydeck-dist-check.XXXXXX")"
cleanup() { rm -rf "$work"; }
trap cleanup EXIT

failures=0
step() { printf '\n== %s\n' "$*"; }
note_fail() { failures=$((failures + 1)); printf 'FAILED: %s\n' "$*"; }

cd "$repo" || exit 1

step "1. build (uv build --offline into a temporary directory)"
if ! uv build --offline --out-dir "$work/dist"; then note_fail "uv build"; fi
wheels=("$work"/dist/*.whl)
if [ ! -f "${wheels[0]}" ] || [ "${#wheels[@]}" -ne 1 ]; then
  note_fail "expected exactly one wheel, found ${#wheels[@]}"
  exit 1
fi
wheel="${wheels[0]}"
echo "wheel: $(basename "$wheel")"

step "2. temporary virtualenv outside the repository"
echo "venv: $work/venv"
if ! uv venv --offline --python 3.13 "$work/venv"; then note_fail "uv venv"; exit 1; fi
py="$work/venv/bin/python"

step "3. install the built wheel offline (uv cache only)"
# The companion's server packages are the `companion` extra, which the docs always install.
if ! uv pip install --offline --python "$py" "relaydeck[companion] @ file://$wheel"; then
  note_fail "install from the wheel"
  exit 1
fi

# Companion commands run with a temporary HOME: the real owner configuration is never read or written.
home="$work/home"
mkdir -p "$home"
run() {
  printf '\n$ %s\n' "$*"
  env HOME="$home" "$@"
  local status=$?
  printf 'exit %s\n' "$status"
  return "$status"
}

step "4. the installed commands"
run "$work/venv/bin/relaydeck" --help || note_fail "relaydeck --help"
run "$work/venv/bin/relaydeck-companion" --help || note_fail "relaydeck-companion --help"
run "$work/venv/bin/relaydeck" setup --help || note_fail "relaydeck setup --help"
run "$work/venv/bin/relaydeck-companion" doctor --help || note_fail "relaydeck-companion doctor --help"

step "5. import every module of every distribution package"
env HOME="$home" "$py" - <<'PY' || note_fail "module imports"
import importlib
import pkgutil
import sys

failed = []
count = 0
for package_name in ("relaydeck", "relaydeck_terminal", "relaydeck_notifications"):
    package = importlib.import_module(package_name)
    names = [package_name]
    for info in pkgutil.walk_packages(package.__path__, prefix=package_name + "."):
        if info.name.endswith("__main__"):
            continue
        names.append(info.name)
    for name in names:
        count += 1
        try:
            importlib.import_module(name)
        except Exception as error:  # noqa: BLE001 - every failure is reported, then the step fails
            failed.append(f"{name}: {type(error).__name__}: {error}")
print(f"imported {count} modules")
for line in failed:
    print("import failed:", line)
sys.exit(1 if failed else 0)
PY

step "6. package data the runtime reads is present in the installed package"
env HOME="$home" "$py" - <<'PY' || note_fail "package data"
import importlib.resources
import sys

required = [
    ("relaydeck.usage.resources", "hook_manifest.json"),
    ("relaydeck.usage.resources", "statusline_limits_hook.py"),
    ("relaydeck.companion", "companion.template.toml"),
    ("relaydeck.client.assets.design_system.fonts", "JetBrainsMono-Regular.ttf"),
    ("relaydeck.client.assets.design_system.fonts", "BricolageGrotesque-Regular.ttf"),
    ("relaydeck_terminal", "py.typed"),
]
missing = []
for package, name in required:
    resource = importlib.resources.files(package).joinpath(name)
    if not resource.is_file():
        missing.append(f"{package}/{name}")
    else:
        print(f"ok {package}/{name} ({resource.stat().st_size} bytes)")
for item in missing:
    print("missing package data:", item)
sys.exit(1 if missing else 0)
PY

step "7. cleanup"
echo "removing $work"

if [ "$failures" -ne 0 ]; then
  printf '\nRESULT: %s step(s) failed\n' "$failures"
  exit 1
fi
printf '\nRESULT: all checks passed\n'
