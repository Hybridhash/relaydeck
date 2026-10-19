# patches/

Every patch to third-party code lives in one removable folder (inputs only, no build outputs). The web build
runs each patch's check before compiling; nothing here edits the Flutter SDK, the pub cache or the Flet cache.
To remove a patch, delete its folder and its delimited block in `scripts/build_web_client.sh` (plus its
`REPAIR_MARKERS` entry in `scripts/build_manifest.py` for the web patches).

| Folder | What and why | Base | Used by |
|---|---|---|---|
| `flutter-file-upload-patch/` | file_picker web fix (upstream PR #2228): picture attach never resolved in WebKit, so on iPhone and iPad. | file_picker 11.0.3 | web build (`--check` preflight) |
| `flutter-ios-pointer-patch/` | Flutter engine PR #189608 backport: iOS WebKit lost the pointer-up and left touches stuck down. Applied to a copy of the SDK; `~/flutter` is never edited. | Flutter 3.44.8 | web build (isolated SDK copy) |
| `flet-reconnect-merge-patch/` | Flet reconnect repairs: snapshots reconcile controls by id (a mounted button no longer stays disabled), and a returning installed web app reconnects at once with capped retry delays. | Flet 1.0.3 | web build (`--check` preflight) |
| `flet-build-template-ios15/` | Flet iOS build template deployment target 13.0 to 15.0 (Xcode 27 supports 15.0+ only). | Flet 1.0.3 template | iOS builds |
| `native-runtime-pins/` | `mobile-overrides.toml`: pins native packaging needs that differ from the host lock. | `uv.lock` | `scripts/build_native.py` |

## Apply and check

```sh
patches/flutter-file-upload-patch/apply.sh --check       # vendored file_picker == pristine + diff
patches/flet-reconnect-merge-patch/apply.sh --check      # vendored Flet == pristine + diff
patches/flet-reconnect-merge-patch/apply.sh --test       # Flutter tests for the reconnect repairs
patches/flutter-ios-pointer-patch/apply.sh [--force | --ensure] [DEST]
patches/flet-build-template-ios15/apply.sh [--force] [DEST]
```

`apply.sh` rebuilds a patched tree from the pristine input and the recorded diff (pristine input via
`$RD_FILE_PICKER_SRC`, `$RD_FLET_SRC` or the pub cache; hashes are verified). The default destination is
`${RD_PATCH_CACHE:-~/Library/Caches/RelayDeck/patches}/<patch>`. The patched file_picker and Flet packages
are vendored under `extensions/terminal/third_party/` and referenced by
`extensions/terminal/examples/viewer/pyproject.toml`; the diff is the source of truth and `--check` proves the
vendored copy has not drifted. None of the scripts uses the network or runs a build.

## Native runtime pins

Nothing here changes the root `pyproject.toml` or `uv.lock`; the pins apply to staged native copies only.

| Need | Host | Native build |
|---|---|---|
| Python for `flet build` | 3.13 | 3.14 (`pypi.flet.dev` has `cryptography` 48.0.0 for cp314 only) |
| `requires-python` of the staged app and both extensions | `>=3.13,<3.14` | `>=3.13,<3.15` |
| `cffi` | 2.1.1 | 2.0.0 (only version with mobile wheels) |
| `msgpack` | 1.2.3 | 1.1.2 (only version with mobile wheels) |
| Flutter override `jni_flutter` | none | 1.0.3 (the Flet template pins `jni: 1.0.0`) |
| Build location | repo under `~/Desktop` | `/private/tmp/rd-native/` (file protection can block build tools under `~/Desktop`) |

```sh
scripts/build_native.py stage --platform android|ios [--replace] [--skip-template]
scripts/build_native.py build --platform android|ios [--run] [--yes] [-- <extra flet args>]
scripts/build_native.py inspect --platform android|ios [PATH]
```

`stage` copies only the client, contracts, domain and both extensions (never server code) and applies the
overrides; for iOS it also runs `flet-build-template-ios15/apply.sh`. `build` prints the exact `flet build`
command and runs it only with `--run` (`--yes` lets Flet install missing tools, so it is opt-in); iOS builds
use no `--arch`. `inspect` fails on server or dev code, tests or secrets in the packaged payload and writes
`payload-manifest.json`. Native apps are in progress; no native build or device run has verified these pins.
