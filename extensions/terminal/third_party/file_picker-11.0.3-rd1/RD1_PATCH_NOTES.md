# RD1: RelayDeck backport of the file_picker web fix

Vendored from `file_picker` **11.0.3** (pub.dev archive; `LICENSE` kept).
Only `lib/src/platform/web/file_picker_web.dart` differs from upstream
11.0.3, plus this note and `test/file_picker_web_backport_test.dart`.
Package name/version stay `file_picker: 11.0.3` so the `^11.0.2`
constraint keeps resolving. Native (Android/iOS/macOS/Windows/Linux)
implementations and the public Dart API are unchanged.

## Why

11.0.3 attached its hidden `<input type=file>` (and the
`flt-file-picker-inputs` container) with `HTMLCollection.add`, which does
not exist in package:web — it resolved to the js_interop `+` operator
and silently never inserted anything. Chromium fires `change` on a
detached input; WebKit (Safari, all iOS browsers) does not, so iPhone
picks never resolved. It also removed the input right after `click()`.

Upstream issue vicajilau/flutter_file_picker#2222, fix PR #2228
(merged 2026-10-05, released as file_picker_web 4.0.1). Flet 1.0.3
pins `file_picker: ^11.0.2` and uses the 11.x API, so we backport the
web-session fix here instead of upgrading. Reference archive
`file_picker_web-4.0.1.tar.gz` sha256
`8d69eb18de3c028a49276d6ca317028a1f39fa17c3ec312e132bac8659c8775e`.

## What changed (`file_picker_web.dart`, all marked RD1)

- Container and input attach with `appendChild`; the input stays
  attached until the session settles (change / cancel / focus-cancel /
  error) and only its own input is removed afterwards.
- JS listener identities (`.toJS` handles) are cached and reused for
  removal, so window/input listeners no longer leak per attempt. The
  duplicate `uploadInput.onChange.listen(...)` subscription is gone;
  the single cached `addEventListener('change', ...)` replaces it.
- Settle-once guard across change/cancel/error paths; empty selections
  complete with an empty result instead of hanging (11.0.3 hung);
  per-file read failures deliver null bytes instead of hanging.
- Focus-before-change delivers a selection already on the input
  instead of reporting cancellation (Safari fires focus first).
- Superseded attempts never touch a newer input (`_activeInput`
  identity check; the newer session retires the older input at start).
- Everything before `uploadInput.click()` stays synchronous (user
  gesture path); `cancelUploadOnWindowBlur: false` still registers no
  window focus listener; the 1s focus grace of 11.0.3 is kept.

## Deliberate deviations from 4.0.1

- Processing errors settle with `null`, not `completeError`: on Flet's
  gesture path there is no error channel, and an error would skip the
  `result` event entirely; null produces the empty result event that
  Python already handles. Same for the empty-selection result (`[]`,
  matching 4.0.1's empty-list completion).
- The focus grace stays 1s (11.0.3) rather than 4.0.1's 500ms.
- `deliverSelection` returns `Future<void>`: both settle paths await
  it, and dart2js rejects awaiting a `void` async function
  (compile error found by a web build).

## Supersede and failure settlement

- Supersession retires synchronously: each attempt is a `_PickSession`
  (input + retirement closure); starting a new pick retires the previous
  session — cached listeners unregistered, future resolved as cancelled
  (null), input removed — before the next input is installed. Later
  events/timers of the retired attempt are ignored via the settled flag,
  removed listeners and the completer guard. 4.0.1 has no supersede
  path (its clearer orphans the old future); this goes beyond it.
- One idempotent failure path: sync read errors, FileReader
  error/abort (`addEventListener`, mirroring 4.0.1 — package:web
  exposes no `onError` stream on FileReader), reader-callback
  exceptions and `deliverSelection` failures all route through
  `failAttempt()`, which settles the attempt null exactly once.
  Whole-attempt null, no partial results (4.0.1 delivers partial
  results and completes errors; RD1 has no error channel on Flet's
  gesture path). `readSequential` is always released via a guarded
  completer, and the read loop exits once the attempt settles.
- `finish()` completes the future BEFORE notifying `onFileLoading`,
  and the notification is guarded: a throwing status listener can
  neither veto completion nor break supersede/retire. (The `picking`
  notification still fails fast to the caller before any DOM work.)
