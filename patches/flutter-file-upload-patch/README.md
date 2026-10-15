# flutter-file-upload-patch: file_picker 11.0.3 "RD1"

Backport of the web file-picker fix so picture attach works in Safari/WebKit
(all iOS browsers). Remove it when Flet ships a file_picker that contains the fix.

- **Why**: file_picker 11.0.3 attaches its hidden `<input type=file>` with
  `HTMLCollection.add`, which does not exist in package:web (it resolves to the
  js_interop `+` operator), so nothing is ever inserted. Chromium fires `change`
  on a detached input; WebKit does not, so iPhone/iPad picks never resolved.
  RD1 also settles each pick once, caches JS listener handles, and delivers a
  selection that arrives before the focus-cancel.
- **Upstream**: issue vicajilau/flutter_file_picker#2222, fix PR #2228 (merged
  2026-10-05, released as `file_picker_web` 4.0.1).
- **Base**: pristine pub.dev `file_picker` **11.0.3** (Flet 1.0.3 pins `^11.0.2`).
- **Changes** (see `file_picker-11.0.3-rd1.diff`): `lib/src/platform/web/file_picker_web.dart`,
  a browser test `test/file_picker_web_backport_test.dart`, and `RD1_PATCH_NOTES.md`
  (full design notes). Native platforms and the public Dart API are unchanged.

## Decision: vendored tree, reproducible by `apply.sh`
The patched package stays **vendored** at
`extensions/terminal/third_party/file_picker-11.0.3-rd1/` because
`extensions/terminal/examples/viewer/pyproject.toml`
(`[tool.flet.flutter.pubspec.dependency_overrides.file_picker]`, `path = "../../../../third_party/file_picker-11.0.3-rd1"`)
points there. The diff in this folder is the source of truth: `apply.sh` rebuilds
that tree from pristine 11.0.3 and the diff, and `apply.sh --check` proves the
vendored copy has not drifted (the web build runs it as a preflight).

## Apply / verify
```
patches/flutter-file-upload-patch/apply.sh --check          # vendored tree == pristine + diff
patches/flutter-file-upload-patch/apply.sh [--force] [DEST] # (re)materialise; default DEST is the vendored path
```
Pristine source: `$RD_FILE_PICKER_SRC` or `${PUB_CACHE:-~/.pub-cache}/hosted/pub.dev/file_picker-11.0.3`.
No network and no build is run by the script.

## Remove (one step, when upstream ships the fix)
1. Delete `patches/flutter-file-upload-patch/`.
2. Delete the `# >>> patch: flutter-file-upload-patch` block in `scripts/build_web_client.sh`
   and the `flutter-file-upload-patch` entry in `REPAIR_MARKERS` (`scripts/build_manifest.py`).
3. Maintainer: delete `extensions/terminal/third_party/file_picker-11.0.3-rd1/` and the
   `dependency_overrides.file_picker` table in `extensions/terminal/examples/viewer/pyproject.toml`,
   then rebuild.

## Verified evidence
Compiled bundle marker: `"abort"` appears 3 times in a patched `main.dart.js`
(1 in unpatched bundles); checked by `scripts/build_manifest.py --check-markers`.
