# relaydeck-terminal extension

Flet user extension bridging the `xterm2` Flutter terminal emulator.
Status: scaffold + protocol complete; Dart side written but **not yet
compiled** (no Flutter SDK in this environment).

- Python control: `src/relaydeck_terminal/terminal.py`
- Wire protocol + incremental decoder: `src/relaydeck_terminal/protocol.py`
- Flutter widget: `src/flutter/relaydeck_terminal/`
- Fixture viewer example: `examples/viewer/` (run with `flet run --web`
  once the Flutter SDK is provisioned; needs `[tool.flet.dev_packages]`)

## Prove it (when a Flutter SDK is available)

```sh
cd src/flutter/relaydeck_terminal
flutter pub get
flutter test
flutter analyze
```
