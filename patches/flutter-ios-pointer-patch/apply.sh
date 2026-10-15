#!/usr/bin/env bash
# Create an ISOLATED patched copy of the Flutter 3.44.8 SDK (engine PR #189608
# web pointer backport + local web build routing). Never edits ~/flutter and
# never runs Flutter, Dart, pod or Gradle.
#
#   apply.sh [--force | --ensure] [DEST]
#
#   --ensure  reuse DEST when it is already this patch level (stamp and every
#             patched file hash match), otherwise rebuild it. For build scripts.
#
#   DEST default: ${RD_PATCH_CACHE:-$HOME/Library/Caches/RelayDeck/patches}/flutter-ios-pointer-patch/flutter-3.44.8-rd189608
#   Source SDK:   $RD_FLUTTER_SDK_SRC, else $HOME/flutter/3.44.8 (read only).
#
# Afterwards DEST/../home/flutter/3.44.8 is a symlink to DEST, so building
# with  HOME=<DEST>/../home  makes Flet (which ignores FLUTTER_BIN and always
# resolves ~/flutter/<version>) use the patched SDK. Real caches are kept by
# pinning PUB_CACHE and RD_FONT_CACHE (scripts/build_web_client.sh does this).
#
# The first `flutter` run inside DEST rebuilds its flutter_tools snapshot
# (the tools patch changes web.dart); that rebuild is a build step and is
# left to the caller.
set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
SRC="${RD_FLUTTER_SDK_SRC:-$HOME/flutter/3.44.8}"
CACHE="${RD_PATCH_CACHE:-$HOME/Library/Caches/RelayDeck/patches}"
FORCE=0
ENSURE=0
DEST=""
for arg in "$@"; do
  case "$arg" in
    --force) FORCE=1 ;;
    --ensure) ENSURE=1 ;;
    -*) echo "unknown option: $arg" >&2; exit 2 ;;
    *) DEST="$arg" ;;
  esac
done
DEST="${DEST:-$CACHE/flutter-ios-pointer-patch/flutter-3.44.8-rd189608}"
DIFFS=(engine-pr189608.diff flutter-web-sdk-lib.diff flutter-tools-web-libs.diff)
UPSTREAM_SHA256="5061ae31d5e6c14c1b9ba6490a4502f51de11f327afd82813f5c20e61d12a00f"
FRAMEWORK_REV_PREFIX="058e0af2c2"

sha() { shasum -a 256 "$1" | awk '{print $1}'; }

[ -x "$SRC/bin/flutter" ] || { echo "Flutter SDK not found at $SRC" >&2; exit 1; }

# Never let DEST be, contain, or sit inside the source SDK: --force/--ensure
# delete DEST, and a typo must not be able to remove ~/flutter.
real() { (cd "$1" 2>/dev/null && pwd -P) || echo "$1"; }
SRC_REAL="$(real "$SRC")"
DEST_PARENT_REAL="$(real "$(dirname "$DEST")")"
DEST_REAL="$DEST_PARENT_REAL/$(basename "$DEST")"
case "$DEST_REAL/" in "$SRC_REAL/"*) echo "DEST is inside the source SDK; refusing" >&2; exit 1 ;; esac
case "$SRC_REAL/" in "$DEST_REAL/"*) echo "DEST contains the source SDK; refusing" >&2; exit 1 ;; esac
# Only ever delete a DEST this script created (it carries our stamp file).
owned() { [ ! -e "$DEST" ] || [ -f "$DEST/.relaydeck-patch-stamp" ] || [ -f "$DEST.partial/.relaydeck-patch-stamp" ]; }
[ "$(sha "$HERE/upstream-pr189608.diff")" = "$UPSTREAM_SHA256" ] || {
  echo "upstream-pr189608.diff does not match its pinned sha256" >&2; exit 1; }
case "$(cat "$SRC/bin/internal/engine.version" 2>/dev/null)" in
  "") echo "cannot read engine.version in $SRC" >&2; exit 1 ;;
esac
# Pristine check: every patched file must still have its recorded pristine hash.
while read -r pristine patched path; do
  case "$pristine" in "#"*|"") continue ;; esac
  [ "$(sha "$SRC/$path")" = "$pristine" ] || {
    echo "source SDK is not pristine 3.44.8 ($path differs); refusing" >&2; exit 1; }
done < "$HERE/expected-sha256.txt"

STAMP="$(cat "$HERE"/upstream-pr189608.diff "${DIFFS[@]/#/$HERE/}" "$HERE/expected-sha256.txt" | shasum -a 256 | awk '{print $1}')"
ready() {
  [ "$(cat "$DEST/.relaydeck-patch-stamp" 2>/dev/null)" = "$STAMP" ] || return 1
  while read -r pristine patched path; do
    case "$pristine" in "#"*|"") continue ;; esac
    [ "$(sha "$DEST/$path" 2>/dev/null)" = "$patched" ] || return 1
  done < "$HERE/expected-sha256.txt"
}
if [ "$ENSURE" = 1 ] && [ -e "$DEST" ] && ready; then
  mkdir -p "$(dirname "$DEST")/home/flutter"
  ln -sfn "$DEST" "$(dirname "$DEST")/home/flutter/3.44.8"
  echo "patched Flutter SDK up to date: $DEST"
  echo "HOME shim for Flet:  $(dirname "$DEST")/home"
  exit 0
fi
if [ -e "$DEST" ]; then
  [ "$FORCE" = 1 ] || [ "$ENSURE" = 1 ] || { echo "$DEST exists; use --force to rebuild it" >&2; exit 1; }
  owned || { echo "$DEST was not created by this script (no stamp); refusing to delete it" >&2; exit 1; }
  rm -rf "$DEST"
fi
mkdir -p "$(dirname "$DEST")"
TMP="$DEST.partial"
rm -rf "$TMP"
# APFS clone when possible (instant, no extra space), plain copy otherwise.
cp -Rc "$SRC" "$TMP" 2>/dev/null || { rm -rf "$TMP"; cp -R "$SRC" "$TMP"; }
trap 'rm -rf "$TMP"' EXIT
for d in "${DIFFS[@]}"; do
  (cd "$TMP" && patch -p1 -s < "$HERE/$d")
done
# Cloned files share storage until written; patch wrote new files, so the
# source tree is unchanged. Drop the stale tools snapshot so Flutter rebuilds
# it from the patched sources on first use.
rm -f "$TMP/bin/cache/flutter_tools.snapshot" "$TMP/bin/cache/flutter_tools.stamp"
while read -r pristine patched path; do
  case "$pristine" in "#"*|"") continue ;; esac
  [ "$(sha "$TMP/$path")" = "$patched" ] || {
    echo "patched $path does not match the recorded hash" >&2; exit 1; }
done < "$HERE/expected-sha256.txt"
printf %s "$STAMP" > "$TMP/.relaydeck-patch-stamp"
mv "$TMP" "$DEST"
trap - EXIT
HOME_SHIM="$(dirname "$DEST")/home"
mkdir -p "$HOME_SHIM/flutter"
ln -sfn "$DEST" "$HOME_SHIM/flutter/3.44.8"
echo "patched Flutter SDK: $DEST"
echo "HOME shim for Flet:  $HOME_SHIM"
