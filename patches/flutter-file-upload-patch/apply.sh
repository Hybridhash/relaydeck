#!/usr/bin/env bash
# Materialise the patched file_picker 11.0.3 (RD1) package from pristine
# pub.dev sources plus file_picker-11.0.3-rd1.diff. Reads only; never touches
# the pub cache. No network, no builds.
#
#   apply.sh [--check] [--force] [DEST]
#
#   DEST   default: REPO_ROOT/extensions/terminal/third_party/file_picker-11.0.3-rd1
#          (the path viewer/pyproject.toml overrides file_picker with)
#   --check  build the patched tree in a temp dir and compare it with DEST;
#            exit 1 on any difference (CI/preflight use). Writes nothing.
#   --force  replace an existing DEST that differs from the regenerated tree.
#
#   PRISTINE source: $RD_FILE_PICKER_SRC, else
#   ${PUB_CACHE:-$HOME/.pub-cache}/hosted/pub.dev/file_picker-11.0.3
set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
ROOT="$(cd "$HERE/../.." && pwd)"
DIFF="$HERE/file_picker-11.0.3-rd1.diff"
PRISTINE="${RD_FILE_PICKER_SRC:-${PUB_CACHE:-$HOME/.pub-cache}/hosted/pub.dev/file_picker-11.0.3}"
CHECK=0
FORCE=0
DEST=""
for arg in "$@"; do
  case "$arg" in
    --check) CHECK=1 ;;
    --force) FORCE=1 ;;
    -*) echo "unknown option: $arg" >&2; exit 2 ;;
    *) DEST="$arg" ;;
  esac
done
DEST="${DEST:-$ROOT/extensions/terminal/third_party/file_picker-11.0.3-rd1}"

[ -f "$PRISTINE/pubspec.yaml" ] || {
  echo "pristine file_picker 11.0.3 not found at $PRISTINE" >&2
  echo "(set RD_FILE_PICKER_SRC, or fetch it into the pub cache first)" >&2
  exit 1
}
grep -q '^version: 11\.0\.3$' "$PRISTINE/pubspec.yaml" || {
  echo "not file_picker 11.0.3: $PRISTINE" >&2
  exit 1
}

TMP="$(mktemp -d "${TMPDIR:-/tmp}/rd-file-picker.XXXXXX")"
trap 'rm -rf "$TMP"' EXIT
# .swiftpm is a pub-cache artefact the vendored tree never carried.
rsync -a --exclude .swiftpm "$PRISTINE/" "$TMP/pkg/"
(cd "$TMP/pkg" && patch -p1 -s -N < "$DIFF")
# Upstream examples, tests and contributor docs are not part of the build.
(cd "$TMP/pkg" && rm -rf example tool test CONTRIBUTING.md)

if [ "$CHECK" = 1 ]; then
  [ -d "$DEST" ] || { echo "missing: $DEST" >&2; exit 1; }
  diff -r "$TMP/pkg" "$DEST" >/dev/null && { echo "file_picker RD1: $DEST matches the patch"; exit 0; }
  echo "file_picker RD1: $DEST differs from pristine + diff" >&2
  diff -rq "$TMP/pkg" "$DEST" >&2 || true
  exit 1
fi

if [ -e "$DEST" ]; then
  if diff -r "$TMP/pkg" "$DEST" >/dev/null 2>&1; then
    echo "file_picker RD1 already up to date: $DEST"
    exit 0
  fi
  [ "$FORCE" = 1 ] || { echo "$DEST exists and differs; use --force" >&2; exit 1; }
  rm -rf "$DEST"
fi
mkdir -p "$(dirname "$DEST")"
mv "$TMP/pkg" "$DEST"
echo "file_picker RD1 materialised: $DEST"
