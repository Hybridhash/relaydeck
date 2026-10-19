#!/usr/bin/env bash
# Materialise the patched flet 1.0.3 package from pristine pub.dev sources plus the diff.
# Reads only: never touches the pub cache, no network, no builds.
# Usage: apply.sh [--check] [--force] [--test] [DEST]; options and sources are in patches/README.md.
set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
ROOT="$(cd "$HERE/../.." && pwd)"
DIFF="$HERE/flet-1.0.3-rd1.diff"
PRISTINE="${RD_FLET_SRC:-${PUB_CACHE:-$HOME/.pub-cache}/hosted/pub.dev/flet-1.0.3}"
CHECK=0
FORCE=0
TEST=0
DEST=""
for arg in "$@"; do
  case "$arg" in
    --check) CHECK=1 ;;
    --force) FORCE=1 ;;
    --test) TEST=1 ;;
    -*) echo "unknown option: $arg" >&2; exit 2 ;;
    *) DEST="$arg" ;;
  esac
done
DEST="${DEST:-$ROOT/extensions/terminal/third_party/flet-1.0.3-rd1}"

[ -f "$PRISTINE/pubspec.yaml" ] || {
  echo "pristine flet 1.0.3 not found at $PRISTINE" >&2
  echo "(set RD_FLET_SRC, or fetch it into the pub cache first)" >&2
  exit 1
}
grep -q '^version: 1\.0\.3$' "$PRISTINE/pubspec.yaml" || {
  echo "not flet 1.0.3: $PRISTINE" >&2
  exit 1
}

TMP="$(mktemp -d "${TMPDIR:-/tmp}/rd-flet-merge.XXXXXX")"
trap 'rm -rf "$TMP"' EXIT
# .swiftpm is a pub-cache artefact the vendored tree never carried.
rsync -a --exclude .swiftpm "$PRISTINE/" "$TMP/pkg/"
(cd "$TMP/pkg" && patch -p1 -s -N < "$DIFF")
# Upstream examples, tests and contributor docs are not part of the build.
(cd "$TMP/pkg" && rm -rf README.md test)

if [ "$TEST" = 1 ]; then
  [ -d "$HERE/test" ] || { echo "no test dir: $HERE/test" >&2; exit 1; }
  cp -r "$HERE/test" "$TMP/pkg/test"
  if (cd "$TMP/pkg" && "${FLUTTER:-$HOME/flutter/3.44.8/bin/flutter}" test); then
    exit 0
  else
    exit 1
  fi
fi

if [ "$CHECK" = 1 ]; then
  [ -d "$DEST" ] || { echo "missing: $DEST" >&2; exit 1; }
  diff -r "$TMP/pkg" "$DEST" >/dev/null && { echo "flet RD1: $DEST matches the patch"; exit 0; }
  echo "flet RD1: $DEST differs from pristine + diff" >&2
  diff -rq "$TMP/pkg" "$DEST" >&2 || true
  exit 1
fi

if [ -e "$DEST" ]; then
  if diff -r "$TMP/pkg" "$DEST" >/dev/null 2>&1; then
    echo "flet RD1 already up to date: $DEST"
    exit 0
  fi
  [ "$FORCE" = 1 ] || { echo "$DEST exists and differs; use --force" >&2; exit 1; }
  rm -rf "$DEST"
fi
mkdir -p "$(dirname "$DEST")"
mv "$TMP/pkg" "$DEST"
echo "flet RD1 materialised: $DEST"
