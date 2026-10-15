# flutter-ios-pointer-patch: Flutter web engine PR #189608 backport

Fixes Flutter web losing the pointer-up on iOS WebKit (iOS 27: WebKit stops
dispatching pointer events for a touch it hands to a native gesture), which left
touches stuck down. Remove it when the Flutter version Flet pins contains PR #189608.

- **Upstream**: flutter/flutter PR #189608; tracking issue flutter/flutter#188781.
- **Base**: Flutter **3.44.8** (framework rev `058e0af2c2`, Dart 3.12.2), pristine at `~/flutter/3.44.8`.
- **Files**
  - `upstream-pr189608.diff`: the fetched upstream diff, kept for provenance only
    (sha256 `5061ae31d5e6c14c1b9ba6490a4502f51de11f327afd82813f5c20e61d12a00f`, checked by `apply.sh`).
  - `engine-pr189608.diff`: its two source files (`dom.dart`, `pointer_binding.dart`) in the engine source mirror
    (`engine/src/flutter/lib/web_ui/lib/src/engine/`); the upstream unit-test hunk is not applied (that test tree is not in the SDK).
  - `flutter-web-sdk-lib.diff`: the same two files in the precompiled web libraries the compiler reads
    (`bin/cache/flutter_web_sdk/lib/_engine/engine/`).
  - `flutter-tools-web-libs.diff`: a **local** build-routing change, not upstream: `flutter_tools`' web target compiles
    with `--libraries-spec` instead of the precompiled `dart2js_platform.dill`, so the patched `dart:ui` sources
    actually reach `main.dart.js`. It is a separate file so the upstream backport stays clean.
  - `expected-sha256.txt`: pristine and patched hashes of the five changed files.
- **Isolation**: `apply.sh` copies the SDK (APFS clone) into a cache dir and patches the copy. It never edits `~/flutter`
  and never runs Flutter. Flet ignores `FLUTTER_BIN` and always resolves `$HOME/flutter/<version>`, so the build selects
  the copy through an isolated `HOME` whose `flutter/3.44.8` symlink points at it (`DEST/../home`, created by `apply.sh`).
  The build script keeps the real `PUB_CACHE`, font cache and `~/.flet` cache.
- **First use**: the copy's `flutter_tools` snapshot is deleted so Flutter rebuilds it from the patched `web.dart` on the
  first `flutter` run inside the copy. That rebuild is a build step; `apply.sh` does not perform it.

## Apply
```
patches/flutter-ios-pointer-patch/apply.sh [--force | --ensure] [DEST]
# default DEST: ${RD_PATCH_CACHE:-~/Library/Caches/RelayDeck/patches}/flutter-ios-pointer-patch/flutter-3.44.8-rd189608
```
`--ensure` reuses an up-to-date copy (stamp + all five hashes match). The script refuses a source SDK that is not
pristine 3.44.8.

## Remove (one step, when Flutter ships PR #189608)
1. Delete `patches/flutter-ios-pointer-patch/` (and its cache dir, which can be large: `rm -rf` the `DEST` and `home`).
2. Delete the `# >>> patch: flutter-ios-pointer-patch` block in `scripts/build_web_client.sh` and the
   `flutter-ios-pointer-patch` entry in `REPAIR_MARKERS` (`scripts/build_manifest.py`); the build then uses the stock SDK.

## Verified evidence
Compiled bundle marker: `"touchend"` 4 and `"touchcancel"` 2 occurrences in a patched `main.dart.js`
(3 and 1 unpatched); checked by `scripts/build_manifest.py --check-markers`.
Reproduce the depfile proof by rebuilding through a copy `apply.sh` makes: the build
depfiles reference the patched copy (893 references here) and never the pristine SDK (0 here).
Whether iOS 27 behaves correctly on a real device is not proven here.
