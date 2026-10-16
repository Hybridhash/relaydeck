# flet-reconnect-merge-patch: Flet 1.0.3 RD1

## Defect and bounded repair

A register-client snapshot rebuilds Control objects inside lists. Their IDs and
properties compare equal, so Flutter's InheritedNotifier retains its listener
on the old object. A later disabled-to-enabled patch reaches the new object;
the mounted Send IconButton remains disabled and emits no click.

The repair recursively reconciles lists, maps and nested lists by Control ID
and type across the existing subtree. Surviving Controls keep their mounted
notifiers through insertion, removal, reordering and reparenting. Result lists
follow source order; structural changes notify the owning parent. Component
Controls retain `notifyParent = true`. Type changes create a new instance.

Removed Controls and descendants are retired from `controlsIndex`, and their
stale event/notification callbacks become no-ops. Pinned `applyPatch`'s REMOVE
branch (`lib/src/models/control.dart`, base lines 403–415) detaches the node
and notifies its owner; it has no Control disposal hook. Widget unmount still
owns listener/resource cleanup (e.g. `IconButtonControl.dispose`, base lines
44–48); shared surviving notifiers are preserved.

No event transport, authentication or replay policy changes.

## Base and source audit

Base: the cached pub.dev `flet` **1.0.3** Dart package, name/version unchanged,
with its LICENSE and upstream files preserved. Base `lib/src/models/control.dart`
SHA-256: `632977fe3b005f15e021d3d21df07c09cb2648fd3b31399b77f9f3365f8112dc`.

`Control.update` is also used for partial updates: `flet_backend.dart` calls it
for register snapshots, route/platform changes and `updateControl` properties.
Omitted keys are therefore **not cleared** by this patch. A snapshot-specific
contract is required to clear stale default-valued properties or omitted
component bodies safely. Explicit empty overlay/dialog lists are reconciled
and retired normally; omitted lists retain partial-update semantics.

## Reproduce and check

The whole package is vendored at
`extensions/terminal/third_party/flet-1.0.3-rd1/`, following the file-picker patch.
`extensions/terminal/examples/viewer/pyproject.toml` overrides `flet` using
`../../../../third_party/flet-1.0.3-rd1` relative to the generated
`build/flutter/pubspec.yaml`.

```sh
patches/flet-reconnect-merge-patch/apply.sh --check
patches/flet-reconnect-merge-patch/apply.sh [--force] [DEST]
```

Pristine input: `$RD_FLET_SRC` or
`${PUB_CACHE:-$HOME/.pub-cache}/hosted/pub.dev/flet-1.0.3`. The script reconstructs
the package from that input and `flet-1.0.3-rd1.diff`, never editing the pub cache
or fetching dependencies. `--check` compares the reconstructed whole tree.

To regenerate the diff after editing the vendored tree (headers carry no
timestamps, matching the original style):

```sh
STAGE=$(mktemp -d /tmp/rd-diffgen.XXXXXX)
rsync -a --exclude .swiftpm --exclude test --exclude README.md \
  "${PUB_CACHE:-$HOME/.pub-cache}/hosted/pub.dev/flet-1.0.3/" "$STAGE/a/"
rsync -a extensions/terminal/third_party/flet-1.0.3-rd1/ "$STAGE/b/"
(cd "$STAGE" && diff -ruN a b; true) | grep -v "^diff -ruN " \
  | sed -E 's/^((---|\+\+\+) [^ ]+)\t.*$/\1/' \
  > patches/flet-reconnect-merge-patch/flet-1.0.3-rd1.diff
rm -rf "$STAGE"
patches/flet-reconnect-merge-patch/apply.sh --check
```

## Fast wake reconnect (second repair in this folder)

When an installed web app returns from the background, a dead socket was
discovered only at the server's ping timeout (the browser sends no pings, the
Flet protocol has none, and the server sends nothing to an idle page), and the
retry delay doubled without a cap.

The repair adds only this to pristine Flet: remote retry delays come from a
pure `ReconnectPolicy` (300 ms, 600 ms, 1.2 s, then a 2 s cap, ±25 % jitter,
reset on wake and on register) instead of unbounded doubling, and the retry is
a cancellable `Timer`. On return, `wake()` cancels a pending retry timer and
runs exactly what the timer would have run (`connect()`) at once; when no
timer is pending it does nothing. Every scheduled retry attempt runs exactly
as pristine would run it at expiry — there is deliberately no in-flight guard
(pristine has none), so a retry is never dropped while another attempt is
still opening. If two attempts overlap, the generation each channel carries
resolves them: a retired socket's late close event or packets are ignored, and
a channel that finishes opening after retirement or disposal is disconnected
without registering, so exactly one transport owns the session. Wake sources are
`visibilitychange` to visible, `pageshow` from the back-forward cache, and
`online`, root app only (deliberately no `focus`). Localhost retry timing,
register/queue-drain logic and the merge repair above are unchanged.

## Tests and compiled evidence

```sh
patches/flet-reconnect-merge-patch/apply.sh --test
```

`--test` rebuilds the package from pristine plus the diff in a temp dir, copies
this folder's `test/` in, and runs `flutter test` with Flutter 3.44.8
(`$FLUTTER` overrides the SDK path). Policy tests pin the delay sequence, cap,
jitter bounds, reset and the 15 s threshold; coordinator tests drive the real
backend with a fake channel (wake with no pending timer does nothing, pending
timer runs now and exactly once, retired generation and packets ignored,
dispose during connect, embedded retry, remote/local retry delays). The merge
widget regression also runs from here: it mounts the real Flet control and
notifier tree, merges an identical snapshot, applies Python's button patch
`[0, 1, 'disabled', false]`, then requires exactly one backend click and retained
Control identity. Additional cases cover Send suffixes, reorder/add/remove, dialog overlays,
component bodies, nested maps/lists, reparenting, type replacement, explicit
empty overlays and preservation of properties omitted by partial updates.

The runtime strings `flet_reconnect_merge_rd1` (emitted by the list branch) and
`flet_reconnect_wake_rd1` (emitted by `wake()`, whose detail lines also log
`action=<none|retry_now> source=<>` for device measurement)
are required in compiled `main.dart.js` by `build_manifest.py --check-markers`
through the shared `relaydeck.webbundle.REPAIR_MARKERS` table. The build script
checks the vendored tree before compiling and checks markers afterward.

## Remove when upstream fixes reconciliation

This folder carries two repairs (merge above, wake reconnect) in the one diff;
upstream fixes for either would split or shrink it rather than remove it whole.
To remove entirely: remove this patch folder, the vendored package, the viewer's `flet` override,
the delimited `flet-reconnect-merge-patch` block in `build_web_client.sh` and its
shared repair-marker entry; then rebuild and run the widget regression against
the upstream package. Keep the independent file-picker and engine repairs.
