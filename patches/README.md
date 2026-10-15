# patches/

Every patch to third-party code lives in one clearly named, removable folder with a README
(why, upstream link, base version, how to apply, how to remove). Inputs only: no build outputs.

| Folder | What | Used by |
|---|---|---|
| `flutter-file-upload-patch/` | file_picker 11.0.3 web fix (upstream PR #2228) | web build (`--check` preflight) |
| `flutter-ios-pointer-patch/` | Flutter engine PR #189608 backport + web build routing | web build (isolated patched SDK) |
| `flet-build-template-ios15/` | Flet 1.0.3 template iOS 15.0 deployment target | iOS builds |
| `native-runtime-pins/` | Python/Flutter pins native packaging needs vs the host lock | native builds |

Removal of a patch = delete its folder and its delimited block in `scripts/build_web_client.sh`
(plus its `REPAIR_MARKERS` entry in `scripts/build_manifest.py`).
