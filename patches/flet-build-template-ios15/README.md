# flet-build-template-ios15: iOS 15.0 deployment target for the Flet 1.0.3 build template

Flet 1.0.3's iOS build template hardcodes iOS 13.0, but Xcode 27 supports iOS 15.0+ only, so
`flet build ios` fails. This patch raises the deployment target to 15.0. Remove it when Flet's template does.

- **Base**: `~/.flet/cache/build-template/v1.0.3/flet-build-template.zip`
  (sha256 `b77a99b6f897dc716027200573b97721b08df04a22b27fcbc1681d957e0428df`, checked by `apply.sh`).
- **Changes** (`flet-build-template-ios15.diff`), under `build/{{cookiecutter.out_dir}}/ios/`:
  `Podfile` (`platform :ios, '15.0'` plus a `post_install` line forcing every pod target's
  `IPHONEOS_DEPLOYMENT_TARGET` to 15.0) and `Runner.xcodeproj/project.pbxproj` (three `13.0` to `15.0`).
- **Reproduce**: `apply.sh` unpacks the pinned pristine zip (hash checked) and applies the diff. Re-running it
  against an existing `DEST` compares with `diff -r` and exits non-zero (asking for `--force`) if it differs.

## Apply
```
patches/flet-build-template-ios15/apply.sh [--force] [DEST]
# default DEST: ${RD_PATCH_CACHE:-~/Library/Caches/RelayDeck/patches}/flet-build-template-ios15/template
flet build ios ... --template DEST --template-dir build
```
No network, no build; the Flet cache is only read.

## Remove
Delete `patches/flet-build-template-ios15/` and drop `--template`/`--template-dir` from the native build command.
(No web-build script line: this patch only affects iOS builds, which are run by a maintainer.)

## Not verified
No iOS build or device run has verified this patch. The diff reproduces the template the iOS builds already use.
