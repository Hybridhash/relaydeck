# native-runtime-pins: what native packaging needs versus the host lock

Records, in one removable place, the differences between the host environment
(Python 3.13, `uv.lock`) and what the Android/iOS packages need. **Nothing here changes
the root `pyproject.toml` or `uv.lock`**; these pins apply to staged native copies only. The data
is in `mobile-overrides.toml`.

| Need | Host | Native build | Why |
|---|---|---|---|
| Python for `flet build` | 3.13 (`>=3.13,<3.14`) | `--python-version 3.14` | `pypi.flet.dev` has `cryptography` 48.0.0 for **cp314 only** (Android and iOS) |
| `requires-python` in the staged app and both extensions | `>=3.13,<3.14` | `>=3.13,<3.15` | the staged project must allow 3.14 |
| `cffi` | 2.1.1 | **2.0.0** | only version with mobile wheels |
| `msgpack` | 1.2.3 | **1.1.2** | only version with mobile wheels |
| `cryptography` | 48.0.0 | 48.0.0 (cp314 wheel) | unchanged |
| Flutter override `jni_flutter` | none | **1.0.3** | the Flet template pins `jni: 1.0.0`; `jni_flutter` 1.0.4+1 needs `jni ^1.1.0` |
| Where to build | repo under `~/Desktop` | `/private/tmp/rd-native/` | antivirus file protection can block build tools writing under `~/Desktop` |

The packaged app's wheels come from `pypi.flet.dev`, so the host lock versions cannot be
used as-is for native builds; the staged copies carry the overrides above.

## Use
`scripts/build_native.py` (stdlib only) applies `mobile-overrides.toml` for you; it is the one source of the values:

```
RD_NATIVE_ROOT=/private/tmp/rd-native      # default; never under ~/Desktop or the repo
scripts/build_native.py stage --platform android|ios [--replace] [--skip-template]
scripts/build_native.py build --platform android|ios [--run] [--yes] [-- <extra flet args>]
scripts/build_native.py inspect --platform android|ios [PATH]
```

- `stage` writes `$RD_NATIVE_ROOT/<platform>/project`: only `relaydeck/{client,contracts,domain}`, `__init__.py`,
  `lease_diagnostics.py`, the client assets and both extensions (never `serve.py`, gateway, connector, companion or
  `devtools`; it asserts that). Runtime pins come from `uv.lock` for the target platform, then the overrides are applied:
  `requires-python = ">=3.13,<3.15"` for the app and both extensions, `cffi==2.0.0`, `msgpack==1.1.2`, and
  `[tool.flet.flutter.pubspec.dependency_overrides] jni_flutter = "1.0.3"`. `source-manifest.json` records the source
  hashes, pins, applied overrides and the template. For iOS it also runs
  `../flet-build-template-ios15/apply.sh` (reads the Flet cache, no network).
- `build` prints the exact `flet build` command and executes nothing; `--run` executes it and then inspects the payload.
  Android: `apk --arch arm64-v8a --flutter-build-args=--debug --python-version 3.14`, `JAVA_HOME`/`ANDROID_HOME` from the
  environment, `~/java` or `~/Android/sdk`. iOS: `ios-simulator --python-version 3.14 --template <patched> --template-dir build`
  and **no `--arch`** (all slices are needed or the Python native modules are not bundled and the app is blank).
  `--device` is not implemented. `--yes` lets Flet install missing tools (downloads), so it is opt-in.
- `inspect` (also run after `--run`) opens the APK or `.app` and its `app.zip`, fails on server/dev code
  (`gateway`, `connector`, `companion`, `devtools`, `serve`, the fake adapters), tests, `.env` files or keys, and writes
  `payload-manifest.json` (sizes, sha256) beside the output.
- Flutter: plain `flet build`; Flet resolves `$HOME/flutter/3.44.8` (never edited). The patched SDK in
  `../flutter-ios-pointer-patch/` is for the web build only and is not used for native apps. The iOS 15 template is
  applied for iOS only; Android keeps Flet's stock template (the patch changes nothing there).

## Remove
Delete this folder (and the override logic in `scripts/build_native.py`) once Flet/PyPI mobile wheels cover the host lock (cp313, current cffi/msgpack)
and the Flet template no longer pins `jni: 1.0.0`. There is no script line to remove.

## Not verified
No native build or resolve has verified these values; they come from comparing the mobile wheel
index against the host lock, read directly (cryptography 48.0.0, cffi 2.1.1, msgpack 1.2.3).
