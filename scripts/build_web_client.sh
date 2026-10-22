#!/usr/bin/env bash
# Build the RelayDeck Flutter web client (Flet shell + RelayDeckTerminal
# extension) into OUT, for FLET_WEB_PATH. Builds from a scratch copy so the
# repository stays free of Flutter build output.
#
#   scripts/build_web_client.sh [OUT] [WORK]
#   FLUTTER_BIN=/path/to/flutter/bin  (optional; prepended to PATH; ignored by
#                                      Flet, see the iOS pointer patch block)
#   RD_PATCH_CACHE=DIR     where patched inputs are materialised
#                          (default ~/Library/Caches/RelayDeck/patches)
#   RD_BUILD_DRY_RUN=1     run the patch preflight only, build nothing
#
# Patches (see patches/README.md). Each patch is one delimited block below
# plus its folder; remove a patch by deleting both. The post-build gate fails
# the build if the compiled main.dart.js lacks a listed patch's repair markers.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
OUT="${1:-/private/tmp/rd-build/out}"
WORK="${2:-$(mktemp -d -t rd-web-build)}"
[ -n "${FLUTTER_BIN:-}" ] && export PATH="$FLUTTER_BIN:$PATH"
REAL_HOME="$HOME"
PATCHES=()      # patch folders whose repair must show up in main.dart.js
FLET_ENV=()     # extra environment for the flet build only
SDK_DIR=""

# >>> patch: flutter-file-upload-patch (file_picker 11.0.3 RD1, upstream PR #2228)
# The vendored tree is what viewer/pyproject.toml overrides file_picker with;
# fail early if it drifted from pristine 11.0.3 + the recorded diff.
"$ROOT/patches/flutter-file-upload-patch/apply.sh" --check
PATCHES+=(flutter-file-upload-patch)
# <<< patch: flutter-file-upload-patch

# >>> patch: flet-reconnect-merge-patch (Flet 1.0.3 identity-safe list merge)
"$ROOT/patches/flet-reconnect-merge-patch/apply.sh" --check
PATCHES+=(flet-reconnect-merge-patch)
# <<< patch: flet-reconnect-merge-patch

# >>> patch: flutter-ios-pointer-patch (Flutter engine PR #189608 backport)
# Flet ignores FLUTTER_BIN and always resolves $HOME/flutter/<version>, so
# the patched SDK copy is selected through an isolated HOME whose
# flutter/3.44.8 points at it. Real pub/font/Flet caches stay in use.
SDK_DIR="$("$ROOT/patches/flutter-ios-pointer-patch/apply.sh" --ensure | sed -n 's/^patched Flutter SDK[^:]*: //p')"
[ -d "$SDK_DIR" ] || { echo "patched Flutter SDK was not created" >&2; exit 1; }
SDK_HOME="$(dirname "$SDK_DIR")/home"
[ -e "$SDK_HOME/.flet" ] || ln -s "$REAL_HOME/.flet" "$SDK_HOME/.flet"
FLET_ENV=(env "HOME=$SDK_HOME" "PUB_CACHE=${PUB_CACHE:-$REAL_HOME/.pub-cache}"
  "RD_FONT_CACHE=${RD_FONT_CACHE:-$REAL_HOME/Library/Caches/RelayDeck/web-fonts}")
PATCHES+=(flutter-ios-pointer-patch)
# <<< patch: flutter-ios-pointer-patch

if [ "${RD_BUILD_DRY_RUN:-0}" = 1 ]; then
  echo "dry run: patch preflight passed (${PATCHES[*]}); nothing was built"
  [ -z "$SDK_DIR" ] || echo "patched SDK: $SDK_DIR"
  exit 0
fi

rsync -a --delete --exclude build --exclude .dart_tool --exclude __pycache__ \
  "$ROOT/extensions/terminal/" "$WORK/terminal/"
(cd "$WORK/terminal/examples/viewer" && ${FLET_ENV[@]+"${FLET_ENV[@]}"} "$ROOT/.venv/bin/flet" build web -o "$OUT" --yes \
  --no-cdn \
  --splash-color "#130F26" \
  --splash-dark-color "#130F26")

# Gate: the repairs must be in the compiled bundle, not just in the inputs.
if [ "${#PATCHES[@]}" -gt 0 ]; then
  "$ROOT/.venv/bin/python" "$ROOT/scripts/build_manifest.py" \
    --check-markers "$OUT/main.dart.js" --patches "$(IFS=,; echo "${PATCHES[*]}")" >/dev/null
fi

# App identity for "Add to Home Screen": full screen, app colours, any
# orientation (phones and iPad, portrait and landscape). Brand art derives
# from the committed mock-derived sources (viewer assets/); the venv python
# has PIL for the resizes.
"$ROOT/.venv/bin/python" - "$OUT" "$WORK/terminal/examples/viewer/src/assets" <<'EOF'
import json, re, sys
from pathlib import Path
from PIL import Image
out = Path(sys.argv[1])
assets = Path(sys.argv[2])
FIELD = (0x13, 0x0F, 0x26, 255)
PURPLE = (0x74, 0x43, 0xF5, 255)

# -- brand icons (overwrite the Flet defaults in place) ------------------
icon = Image.open(assets / "icon.png").convert("RGBA")
assert icon.size == (1024, 1024), icon.size
(icon.resize((32, 32), Image.LANCZOS)).save(out / "favicon.png")
# Browsers probe /favicon.ico before any <link>; ship a multi-size one too.
icon.save(out / "favicon.ico", sizes=[(16, 16), (32, 32), (48, 48)])
for size in (192, 512):
    icon.resize((size, size), Image.LANCZOS).save(out / "icons" / f"Icon-{size}.png")
for size in (192, 512):
    # Maskable: tile floats on the dark field inside the 80% safe circle.
    canvas = Image.new("RGBA", (size, size), FIELD)
    t = int(size * 0.52)
    small = icon.resize((t, t), Image.LANCZOS)
    canvas.alpha_composite(small, ((size - t) // 2, (size - t) // 2))
    canvas.convert("RGB").save(out / "icons" / f"Icon-maskable-{size}.png")
# Apple touch: opaque purple, glyph tile blended in (iOS masks corners).
touch = Image.new("RGBA", (180, 180), PURPLE)
t = int(180 * 0.72)
small = icon.resize((t, t), Image.LANCZOS)
touch.alpha_composite(small, ((180 - t) // 2, (180 - t) // 2))
touch.convert("RGB").save(out / "icons" / "apple-touch-icon.png")
stale_touch = out / "icons" / "apple-touch-icon-192.png"
if stale_touch.is_file():
    stale_touch.unlink()  # superseded by apple-touch-icon.png above

# -- splash fallback (Flet normally emits these from assets/splash.png) --
splash = Image.open(assets / "splash.png").convert("RGBA")
for name, density in (("1x", 1), ("2x", 2), ("3x", 3), ("4x", 4)):
    px = splash.width * density // 4  # same width*density/4 rule Flet uses
    for scheme in ("light", "dark"):
        target = out / "splash" / "img" / f"{scheme}-{name}.png"
        if not target.is_file():
            target.parent.mkdir(parents=True, exist_ok=True)
            splash.resize((px, px), Image.LANCZOS).save(target)
            print(f"splash fallback rendered: {target.relative_to(out)}")

# -- manifest + index -----------------------------------------------------
manifest = out / "manifest.json"
doc = json.loads(manifest.read_text())
doc.update(
    name="RelayDeck",
    short_name="RelayDeck",
    description="Your Herdr agents and terminals, from your phone.",
    display="standalone",
    background_color="#130F26",
    theme_color="#130F26",
    orientation="any",
)
manifest.write_text(json.dumps(doc, indent=4) + "\n")
index = out / "index.html"
html = index.read_text()
html = re.sub(r'(name="apple-mobile-web-app-title" content=")[^"]*', r"\1RelayDeck", html)
html = re.sub(r'(name="apple-mobile-web-app-status-bar-style" content=")[^"]*', r"\1black", html)
if 'name="apple-mobile-web-app-capable"' not in html:
    html = html.replace("<head>", '<head>\n  <meta name="apple-mobile-web-app-capable" content="yes">', 1)
html = re.sub(r"<title>[^<]*</title>", "<title>RelayDeck</title>", html)
if 'rel="apple-touch-icon"' in html:
    html = re.sub(r'<link rel="apple-touch-icon" href="[^"]*">',
                  '<link rel="apple-touch-icon" href="icons/apple-touch-icon.png">', html)
else:
    html = html.replace("</head>", '  <link rel="apple-touch-icon" href="icons/apple-touch-icon.png">\n</head>', 1)
# Tab icons: small PNG + a large one (mobile tab lists prefer bigger icons).
TAB_ICONS = (
    '<link rel="icon" type="image/png" sizes="32x32" href="favicon.png">\n'
    '  <link rel="icon" type="image/png" sizes="192x192" href="icons/Icon-192.png">'
)
html = re.sub(r'<link rel="icon"[^>]*>(\s*<link rel="icon"[^>]*>)*', TAB_ICONS, html, count=1)
if 'sizes="192x192" href="icons/Icon-192.png"' not in html:
    html = html.replace("</head>", "  " + TAB_ICONS + "\n</head>", 1)
# Splash field: RelayDeck dark in both schemes (only inside the splash block).
m = re.search(r'(<style id="splash-screen-style">)(.*?)(</style>)', html, re.S)
assert m, "splash style block missing"
block = re.sub(r"background-color: #[0-9a-fA-F]{6};", "background-color: #130F26;", m.group(2))
html = html[:m.start(2)] + block + html[m.end(2):]
index.write_text(html)
EOF

# Fallback fonts for the no-CDN build: the engine fetches
# fallback fonts from <fontFallbackBaseUrl><family>/v<N>/<file>, which is
# assets/fonts/ locally. Extract the engine's path list from the built
# main.dart.js (version segments move with Flutter) and vendor each file
# from gstatic through a persistent cache. All five CJK families are included
# by default; RD_WEB_CJK_FONTS=0 excludes all five for an explicit small build.
# Font validation or missing copyright/OFL notices fails the build.
RD_FONT_CACHE="${RD_FONT_CACHE:-$HOME/Library/Caches/RelayDeck/web-fonts}"
"$ROOT/.venv/bin/python" "$ROOT/scripts/web_fonts.py" "$OUT" "$RD_FONT_CACHE" "${RD_WEB_CJK_FONTS:-1}"

# the pinned-shell service worker. Runs after every other post-process
# step so its BUILD digest covers the final shell files. It replaces Flet's stub at the same URL,
# removes Flutter's own worker registration, and writes the kill switch (see
# scripts/web_service_worker.py).
"$ROOT/.venv/bin/python" "$ROOT/scripts/web_service_worker.py" "$OUT" >/dev/null
"$ROOT/.venv/bin/python" "$ROOT/scripts/web_service_worker.py" "$OUT" --verify >/dev/null

# Reproducibility record (outside the repo): commit, patch hashes, SDK, locks,
# fonts, output hashes.
"$ROOT/.venv/bin/python" "$ROOT/scripts/build_manifest.py" --web-out "$OUT" \
  ${SDK_DIR:+--flutter-sdk "$SDK_DIR"} --out "${OUT%/}.build-manifest.json"

echo "web client ready: $OUT (manifest: ${OUT%/}.build-manifest.json)"
