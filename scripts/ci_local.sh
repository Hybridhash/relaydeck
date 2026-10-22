#!/usr/bin/env zsh
# Run what .github/workflows/ci.yml runs, in the same order, and stop at the first failure.
#   scripts/ci_local.sh            validate, Python tests, Flutter tests
#   scripts/ci_local.sh --fast     validate only (lock, versions, lint, format, types, scans)
#   scripts/ci_local.sh --stress   also run the timing-sensitive tests three times in parallel
# FLUTTER overrides the Flutter binary (default: flutter on PATH, else ~/flutter/<pinned>/bin).
set -euo pipefail

ROOT=${0:A:h:h}
cd "$ROOT"
export UV_PYTHON=3.13 UV_PYTHON_PREFERENCE=only-managed
PINNED_FLUTTER=$(sed -n 's/^  FLUTTER_VERSION: "\(.*\)"/\1/p' .github/workflows/ci.yml)
FLUTTER=${FLUTTER:-$(command -v flutter || echo "$HOME/flutter/$PINNED_FLUTTER/bin/flutter")}
LOGS=${TMPDIR:-/tmp}/relaydeck-ci-local
mkdir -p "$LOGS"
FAST=0 STRESS=0
for arg in "$@"; do
  case $arg in
    --fast) FAST=1 ;;
    --stress) STRESS=1 ;;
    *) echo "unknown option: $arg" >&2; exit 2 ;;
  esac
done

# Timing-sensitive tests: budgets, polling, reconnect and refresh flows.
STRESS_TESTS=(
  tests/unit/test_gateway_metrics.py
  tests/ui/test_server_health.py
  tests/unit/test_usage_pane_limits.py
  tests/integration/test_flet_reconnect_continuity.py
  tests/integration/test_session_store_threads.py
)

started=$SECONDS
step() {
  local name=$1; shift
  local log="$LOGS/${name// /_}.log"
  printf '%-44s' "$name"
  local t0=$SECONDS
  if "$@" >"$log" 2>&1; then
    echo "ok   ($((SECONDS - t0)) s)"
  else
    echo "FAIL ($((SECONDS - t0)) s)"
    tail -40 "$log"
    echo "\nFull log: $log"
    exit 1
  fi
}

echo "== validate"
step "locked dependency resolution" uv sync --locked --extra companion --group dev
step "pinned environment check" uv run python scripts/verify_versions.py
step "lint" uv run ruff check --no-cache .
step "format check" uv run ruff format --check --no-cache .
step "static types" uv run mypy src
step "secret scan" uv run python scripts/scan_release_secrets.py
step "license inventory" uv run python scripts/license_inventory.py --out "$LOGS/sbom.json"
step "dependency vulnerability audit" zsh -c 'uv export --locked --no-hashes --no-emit-project --extra companion --format requirements-txt > "$0" && uvx pip-audit==2.9.0 -r "$0" --no-deps --disable-pip' "$LOGS/requirements-audit.txt"
(( FAST )) && { echo "validate passed in $((SECONDS - started)) s"; exit 0 }

echo "== python tests"
step "unit and architecture" uv run pytest tests/unit tests/architecture -q -p no:cacheprovider
step "contract, integration, security" \
  uv run pytest tests/contracts tests/integration tests/security -q -p no:cacheprovider
step "ui scenarios (headless)" uv run pytest tests/ui -q -p no:cacheprovider

echo "== flutter tests ($PINNED_FLUTTER)"
[[ -x $FLUTTER ]] || { echo "Flutter not found at $FLUTTER (set FLUTTER=...)"; exit 1 }
actual=$("$FLUTTER" --version 2>/dev/null | sed -n 's/^Flutter \([0-9.]*\).*/\1/p' | head -1)
[[ $actual == "$PINNED_FLUTTER" ]] || { echo "Flutter $actual, CI pins $PINNED_FLUTTER"; exit 1 }
terminal=extensions/terminal/src/flutter/relaydeck_terminal
notifications=extensions/notifications/src/flutter/relaydeck_notifications
step "terminal extension pub get (lockfile)" \
  zsh -c "cd $terminal && '$FLUTTER' pub get --enforce-lockfile"
step "terminal extension tests" zsh -c "cd $terminal && '$FLUTTER' test"
step "flet paste menu test" \
  zsh -c "cd $terminal && '$FLUTTER' test --no-pub '$ROOT/tests/native/flet_paste_menu_test.dart'"
step "notifications extension pub get (lockfile)" \
  zsh -c "cd $notifications && '$FLUTTER' pub get --enforce-lockfile"
step "notifications extension tests" zsh -c "cd $notifications && '$FLUTTER' test"
step "reconnect patch tests" env FLUTTER="$FLUTTER" bash patches/flet-reconnect-merge-patch/apply.sh --test

if (( STRESS )); then
  echo "== stress (3 parallel runs)"
  stress() {
    local pids=() i
    for i in 1 2 3; do
      uv run pytest "${STRESS_TESTS[@]}" -q -p no:cacheprovider >"$LOGS/stress_$i.log" 2>&1 &
      pids+=($!)
    done
    local rc=0
    for i in $pids; do wait $i || rc=1; done
    return $rc
  }
  step "timing-sensitive tests x3" stress
fi

echo "all CI steps passed in $((SECONDS - started)) s"
