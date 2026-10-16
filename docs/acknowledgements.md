# Acknowledgements — third-party licenses

Inventory of third-party software that RelayDeck distributes, vendors, patches
or uses to build. Python entries are generated from `uv.lock` (sha256
`500b989977713f80e34060c3e0d1a779098eb4d52d1ce9be54534b10f47b95f7`) with
`uv run python scripts/license_inventory.py` (add `--out PATH` for the JSON
SBOM): 67 Python and 12 Dart entries, 0 unknown. The scan fails
on any UNKNOWN license, and this file is its written proof, so extend both
together when a dependency changes. Sections 3, 4, 6 and 7 below are
maintained by hand; the script does not read them. SPDX ids are measured from installed
distribution metadata, trove classifiers, bundled license files, or the
verified tables in `scripts/license_inventory.py`. Full license texts live
upstream (see the Proof/Source columns) or are bundled where stated.

Upstream sources: the iOS reference client at
`cdc0cdfab127944a67d5c3ec1cf08ae107987712` (Apache-2.0; behavior
reimplemented, no code copied), Herdr schema baseline tag `0.9.1` / protocol 22
(Apache-2.0), Flet 1.0.3 source `a87ca7fc8a813b9d821858083c7539e8e79ab3ba`
(Apache-2.0).

The `relaydeck` package is MIT-licensed (see `LICENSE`); the `relaydeck-terminal`
and `relaydeck-notifications` extensions are Apache-2.0.

The list in Settings › Acknowledgements
(`src/relaydeck/client/features/settings/acknowledgements.py`) is the user-facing
summary of the packages and fonts below; update it with this file.

## 1. Python runtime packages (the closure the native app ships)

Resolved from the runtime `dependencies` of `pyproject.toml` (flet, flet-secure-storage,
relaydeck-terminal, relaydeck-notifications, httpx, websockets, cryptography)
through `uv.lock`. Native builds use the mobile pins in
`patches/native-runtime-pins/` instead of the host lock for `cffi` (2.0.0) and
`msgpack` (1.1.2). The rows below are the licences of the host-locked versions;
the mobile pin versions are not measured separately.

| Package | Locked version | License | How verified |
|---|---|---|---|
| anyio | 4.15.1 | MIT | License-Expression |
| certifi | 2026.7.22 | MPL-2.0 | License metadata field |
| cffi | 2.1.1 | MIT-0 | License-Expression |
| cryptography | 48.0.0 | Apache-2.0 OR BSD-3-Clause | License-Expression |
| flet | 1.0.3 | Apache-2.0 | License-Expression |
| flet-secure-storage | 1.0.3 | Apache-2.0 | bundled license file flet_secure_storage-1.0.3.dist-info/licenses/LICENSE |
| h11 | 0.16.0 | MIT | License metadata field |
| httpcore | 1.0.9 | BSD-3-Clause | License-Expression |
| httpx | 0.28.1 | BSD-3-Clause | License metadata field |
| idna | 3.20 | BSD-3-Clause | License-Expression |
| msgpack | 1.2.3 | Apache-2.0 | License-Expression |
| oauthlib | 4.0.0 | BSD-3-Clause | License metadata field |
| pycparser | 3.0 | BSD-3-Clause | License-Expression |
| relaydeck-notifications | 0.1.0 | Apache-2.0 | License metadata field |
| relaydeck-terminal | 0.1.0 | Apache-2.0 | License metadata field |
| repath | 0.9.0 | MIT | License metadata field |
| six | 1.17.0 | MIT | License metadata field |
| typing-extensions | 4.16.0 | PSF-2.0 | License-Expression |
| websockets | 16.0 | BSD-3-Clause | License-Expression |

## 2. Python packages added on the Mac (`companion` extra)

Not part of the phone apps. They run in the companion (`relaydeck[companion]`,
including `uvicorn`'s optional extras as resolved in the lock).

| Package | Locked version | License | How verified |
|---|---|---|---|
| annotated-types | 0.8.0 | MIT | License-Expression |
| click | 8.5.0 | BSD-3-Clause | License-Expression |
| colorama | 0.4.6 | BSD | verified table (not installed): PyPI colorama/0.4.6 trove classifier BSD License; uv.lock marker sys_platform == 'win32' (absent on macOS/Linux) |
| fastapi | 0.117.1 | MIT | trove classifier: License :: OSI Approved :: MIT License |
| flet-web | 1.0.3 | Apache-2.0 | License-Expression |
| httptools | 0.8.0 | MIT | License-Expression |
| pillow | 12.3.0 | MIT-CMU | License-Expression |
| pydantic | 2.13.5 | MIT | License-Expression |
| pydantic-core | 2.46.5 | MIT | License-Expression |
| python-dotenv | 1.2.4 | BSD-3-Clause | License metadata field |
| pyyaml | 6.0.3 | MIT | License metadata field |
| qrcode | 8.2 | BSD | License metadata field |
| starlette | 0.48.0 | BSD-3-Clause | License-Expression |
| typing-inspection | 0.4.4 | MIT | License-Expression |
| uvicorn | 0.35.0 | BSD-3-Clause | License-Expression |
| uvloop | 0.23.0 | MIT License | License metadata field |
| watchfiles | 1.3.0 | MIT | License metadata field |

## 3. Vendored and patched third-party code

| Component | License | What RelayDeck does | Where |
|---|---|---|---|
| `file_picker` 11.0.3 (Miguel Ruivo, pub.dev) | MIT, (c) 2018 Miguel Ruivo; license file ships in the tree | Vendored with the "RD1" web backport of upstream fix PR `vicajilau/flutter_file_picker#2228` (issue #2222, released upstream as `file_picker_web` 4.0.1) so picture attach works in Safari/WebKit. Remove when Flet ships the fix. | `extensions/terminal/third_party/file_picker-11.0.3-rd1/`; diff and notes in `patches/flutter-file-upload-patch/` |
| Flutter web engine, PR `flutter/flutter#189608` (tracking issue #188781) | BSD-3-Clause (Flutter) | Backported to an isolated copy of Flutter 3.44.8 so touch pointer-up is not lost on iOS WebKit; the patched engine code is compiled into the web bundle. The SDK itself is not redistributed and `~/flutter` is never edited. | `patches/flutter-ios-pointer-patch/` (upstream diff kept for provenance) |
| Flet 1.0.3 build template | Apache-2.0 (Flet) | Patched copy raises the iOS deployment target from 13.0 to 15.0 (Xcode 27 needs 15.0+); only the diff is kept, the template is not redistributed. | `patches/flet-build-template-ios15/` |
| Native packaging pins | n/a (data) | Python 3.14 for mobile builds, mobile-wheel versions of `cffi` and `msgpack`, `jni_flutter` 1.0.3 override. | `patches/native-runtime-pins/` |

## 4. Fonts

Bundled fonts are served from the app's own assets; no font is fetched at
runtime. Each is under the SIL Open Font License 1.1; the license text of each
family ships next to the fonts. Provenance, sources and hashes:
`src/relaydeck/client/assets/design_system/PROVENANCE.md`.

| Family | Files | Copyright | License text |
|---|---|---|---|
| Bricolage Grotesque v9 (latin subset) | `BricolageGrotesque-{Regular,Medium,SemiBold,Bold,ExtraBold}.ttf` | 2022 The Bricolage Grotesque Project Authors (https://github.com/ateliertriay/bricolage) | `design_system/OFL-BricolageGrotesque.txt` |
| JetBrains Mono v24 (latin subset) | `JetBrainsMono-{Regular,SemiBold,Bold}.ttf` | 2020 The JetBrains Mono Project Authors (https://github.com/JetBrains/JetBrainsMono) | `design_system/OFL-JetBrainsMono.txt` |
| Noto Sans Symbols 2 (subset to the symbol ranges agents emit; modified by subsetting) | `NotoSansSymbols2-Regular.ttf` | 2022 The Noto Project Authors (https://github.com/notofonts/symbols) | `design_system/OFL-NotoSansSymbols2.txt` |

Web build fallback fonts: `scripts/web_fonts.py` downloads the fallback font
files the Flutter web engine references (Noto Sans families from
`fonts.gstatic.com`; CJK families excluded by default) and validates each file
before it enters the build or its cache. They are not committed and are not
listed here individually; upstream distributes the Noto families under the SIL
Open Font License 1.1. The one committed sample, the test fixture
`tests/fixtures/fallback-sample.woff2` (a Noto Sans Lycian subset), has its
notice in `tests/fixtures/OFL-fallback-sample.txt`.

## 5. Python build and development tooling (not shipped)

| Package | Locked version | License | How verified |
|---|---|---|---|
| arrow | 1.4.0 | Apache-2.0 | trove classifier: License :: OSI Approved :: Apache Software License |
| binaryornot | 0.4.4 | BSD | License metadata field |
| chardet | 5.2.0 | LGPL | License metadata field |
| charset-normalizer | 3.5.2 | MIT | License metadata field |
| cookiecutter | 2.7.1 | BSD-3-Clause | bundled license file cookiecutter-2.7.1.dist-info/licenses/LICENSE |
| flet-cli | 1.0.3 | Apache-2.0 | License-Expression |
| flet-desktop | 1.0.3 | Apache-2.0 | License-Expression |
| flet-platform-assets | 1.0.3 | Apache-2.0 | License-Expression |
| iniconfig | 2.3.0 | MIT | License-Expression |
| jinja2 | 3.1.6 | BSD | trove classifier: License :: OSI Approved :: BSD License |
| librt | 0.16.0 | MIT | License-Expression |
| markdown-it-py | 4.2.0 | MIT | trove classifier: License :: OSI Approved :: MIT License |
| markupsafe | 3.0.4 | BSD-3-Clause | License-Expression |
| mdurl | 0.1.2 | MIT | trove classifier: License :: OSI Approved :: MIT License |
| mypy | 1.20.2 | MIT | License-Expression |
| mypy-extensions | 1.1.0 | MIT | License-Expression |
| packaging | 26.3 | Apache-2.0 OR BSD-2-Clause | License-Expression |
| pathspec | 1.1.1 | MPL-2.0 | trove classifier: License :: OSI Approved :: Mozilla Public License 2.0 (MPL 2.0) |
| pluggy | 1.6.0 | MIT | License metadata field |
| pygments | 2.21.0 | BSD-2-Clause | License-Expression |
| pytest | 8.4.2 | MIT | License metadata field |
| python-dateutil | 2.9.0.post0 | Dual License | License metadata field |
| python-slugify | 9.1.2 | MIT | License-Expression |
| requests | 2.34.2 | Apache-2.0 | License metadata field |
| rich | 15.0.0 | MIT | License metadata field |
| ruff | 0.12.12 | MIT | trove classifier: License :: OSI Approved :: MIT License |
| text-unidecode | 1.3 | Artistic License | License metadata field |
| tzdata | 2026.4 | Apache-2.0 | License metadata field |
| urllib3 | 2.8.0 | MIT | License-Expression |
| watchdog | 6.0.0 | Apache-2.0 | License metadata field |

Copyleft/dual-license notes (measured):

- `certifi 2026.7.22` is MPL-2.0 (file-scoped copyleft) and IS in the runtime
  closure via `httpx`/`httpcore` (per `uv.lock` dependency edges). MPL-2.0 permits
  distribution in a larger work under a different license; only the
  MPL-licensed files themselves remain under MPL-2.0.
- `chardet 5.2.0` (LGPL per its `License` metadata field) enters via the `dev`
  group (`flet-cli` -> `cookiecutter`); packaging tooling, not in the runtime closure.
- `text-unidecode 1.3` (`Artistic License` metadata field; dual Artistic/GPL on
  PyPI) enters via `python-slugify` <- `cookiecutter` (dev tooling only).
- `pathspec 1.1.1` (MPL-2.0) enters via `mypy` (`dev` group): build-time only.

## 6. Dart / native dependencies (per extension pubspec)

| Package | Constraint | License | Proof | Used by |
|---|---|---|---|---|
| app_links | ^7.2.1 | Apache-2.0 | verified table: llfbandit/app_links LICENSE (Apache-2.0) | relaydeck_notifications |
| firebase_core | ^4.15.0 | BSD-3-Clause | verified table: flutterfire LICENSE (BSD, (c) 2017 The Chromium Authors) | relaydeck_notifications |
| firebase_messaging | ^16.7.0 | BSD-3-Clause | verified table: flutterfire LICENSE (BSD, (c) 2017 The Chromium Authors) | relaydeck_notifications |
| flet | ^1.0.3 | Apache-2.0 | verified table: flet-dev/flet LICENSE at a87ca7fc (Apache-2.0) | relaydeck_notifications |
| flutter | (sdk) | BSD-3-Clause | Flutter SDK (flutter/flutter LICENSE); provided by pinned Flutter 3.44.8, not a package | relaydeck_notifications |
| flutter_lints | ^3.0.0 (dev) | BSD-3-Clause | verified table: dart-lang/lints LICENSE (BSD, (c) 2021 Dart project authors) | relaydeck_notifications |
| flutter_test |  (dev) | BSD-3-Clause | Flutter SDK (flutter/flutter LICENSE); provided by pinned Flutter 3.44.8, not a package | relaydeck_notifications |
| flet | ^1.0.3 | Apache-2.0 | verified table: flet-dev/flet LICENSE at a87ca7fc (Apache-2.0) | relaydeck_terminal |
| flutter | (sdk) | BSD-3-Clause | Flutter SDK (flutter/flutter LICENSE); provided by pinned Flutter 3.44.8, not a package | relaydeck_terminal |
| flutter_lints | ^3.0.0 (dev) | BSD-3-Clause | verified table: dart-lang/lints LICENSE (BSD, (c) 2021 Dart project authors) | relaydeck_terminal |
| flutter_test |  (dev) | BSD-3-Clause | Flutter SDK (flutter/flutter LICENSE); provided by pinned Flutter 3.44.8, not a package | relaydeck_terminal |
| web | ^1.1.1 | BSD-3-Clause | verified table: pub.dev archive web-1.1.1 LICENSE (BSD, (c) 2023 Dart project authors) | relaydeck_terminal |
| xterm2 | ^5.2.0 | MIT | verified table: pub.dev archive xterm2-5.2.0 LICENSE (MIT, (c) 2020 xuty) | relaydeck_terminal |

Transitive Dart dependencies are pinned in
`extensions/terminal/src/flutter/relaydeck_terminal/pubspec.lock` (about 140
packages) and are not listed here; their licences are those of each package on
pub.dev. `extensions/notifications` has no `pubspec.lock`. Native Android and
iOS dependencies, including the Firebase SDKs, resolve at build time and are
likewise not listed.

## 7. Notices

- xterm2 (MIT, (c) 2020 xuty): the MIT permission notice ships with the
  package; see https://github.com/SoFluffyOS/xterm2.
- Firebase Flutter plugins (BSD-3-Clause, (c) 2017 The Chromium Authors): see
  https://github.com/firebase/flutterfire/blob/main/LICENSE.
- app_links (Apache-2.0): see https://github.com/llfbandit/app_links.
- file_picker (MIT, (c) 2018 Miguel Ruivo): the license file ships in the vendored tree.
- Flet SDK/CLI/tooling (Apache-2.0): see
  https://github.com/flet-dev/flet/tree/a87ca7fc8a813b9d821858083c7539e8e79ab3ba.
- Fonts: see section 4 and the bundled `OFL-*.txt` files.
- BSD/MIT/Apache-2.0/MPL-2.0 Python packages above: see the PyPI project page
  for each pinned version for the full text.
