# relaydeck-notifications extension

Flet user extension bridging Firebase Cloud Messaging and Android app
links. Status: scaffold complete; Dart side written but **not yet
compiled** (no Flutter SDK here).

Firebase project setup is a separately authorized owner step
: do NOT create a Firebase project, download
`google-services.json`, or register sender IDs without approval. The
`google-services.json` file must never be committed.

## Prove it (when provisioned)

```sh
cd src/flutter/relaydeck_notifications
flutter pub get
flutter test
flutter analyze
```

Device proof additionally needs a test Firebase project, a release-mode
APK on a physical device, and the device matrix (background, kill,
force-stop, reboot, Doze, rotation, reinstall, denied permission).
