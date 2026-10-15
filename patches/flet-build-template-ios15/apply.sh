#!/usr/bin/env bash
# Materialise the Flet 1.0.3 build template with the iOS 15.0 deployment
# target from the pristine cached template zip plus the diff. No network,
# no builds; the Flet cache is only read.
#
#   apply.sh [--force] [DEST]
#
#   DEST default: ${RD_PATCH_CACHE:-$HOME/Library/Caches/RelayDeck/patches}/flet-build-template-ios15/template
#   Use it as:  flet build ... --template DEST --template-dir build
#   Pristine zip: $RD_FLET_TEMPLATE_ZIP, else
#   ~/.flet/cache/build-template/v1.0.3/flet-build-template.zip (sha256 pinned below)
set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
DIFF="$HERE/flet-build-template-ios15.diff"
ZIP="${RD_FLET_TEMPLATE_ZIP:-$HOME/.flet/cache/build-template/v1.0.3/flet-build-template.zip}"
ZIP_SHA256="b77a99b6f897dc716027200573b97721b08df04a22b27fcbc1681d957e0428df"
CACHE="${RD_PATCH_CACHE:-$HOME/Library/Caches/RelayDeck/patches}"
FORCE=0
DEST=""
for arg in "$@"; do
  case "$arg" in
    --force) FORCE=1 ;;
    -*) echo "unknown option: $arg" >&2; exit 2 ;;
    *) DEST="$arg" ;;
  esac
done
DEST="${DEST:-$CACHE/flet-build-template-ios15/template}"

[ -f "$ZIP" ] || { echo "pristine template zip not found: $ZIP" >&2; exit 1; }
actual="$(shasum -a 256 "$ZIP" | awk '{print $1}')"
[ "$actual" = "$ZIP_SHA256" ] || {
  echo "template zip sha256 mismatch (expected $ZIP_SHA256, got $actual):" >&2
  echo "  Flet's template changed; re-derive this patch before building." >&2
  exit 1
}

TMP="$(mktemp -d "${TMPDIR:-/tmp}/rd-flet-template.XXXXXX")"
trap 'rm -rf "$TMP"' EXIT
mkdir "$TMP/template"
(cd "$TMP/template" && unzip -q "$ZIP" && patch -p1 -s < "$DIFF")

if [ -e "$DEST" ]; then
  if diff -r "$TMP/template" "$DEST" >/dev/null 2>&1; then
    echo "flet template iOS15 already up to date: $DEST"
    exit 0
  fi
  [ "$FORCE" = 1 ] || { echo "$DEST exists and differs; use --force" >&2; exit 1; }
  rm -rf "$DEST"
fi
mkdir -p "$(dirname "$DEST")"
mv "$TMP/template" "$DEST"
echo "flet template iOS15 materialised: $DEST"
