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

## Tests and compiled evidence

Run `flutter test test/widgets/reconnect_merge_test.dart` in the vendored
package with Flutter 3.44.8. The widget regression mounts the real Flet control
and notifier tree, merges an identical snapshot, applies Python's button patch
`[0, 1, 'disabled', false]`, then requires exactly one backend click and retained
Control identity. Additional cases cover Send suffixes, reorder/add/remove, dialog overlays,
component bodies, nested maps/lists, reparenting, type replacement, explicit
empty overlays and preservation of properties omitted by partial updates.

The runtime string `flet_reconnect_merge_rd1` is emitted by the list branch;
`build_manifest.py --check-markers` requires it in compiled `main.dart.js`
through the shared `relaydeck.webbundle.REPAIR_MARKERS` table. The build script
checks the vendored tree before compiling and checks markers afterward.

## Remove when upstream fixes reconciliation

Remove this patch folder, the vendored package, the viewer's `flet` override,
the delimited `flet-reconnect-merge-patch` block in `build_web_client.sh` and its
shared repair-marker entry; then rebuild and run the widget regression against
the upstream package. Keep the independent file-picker and engine repairs.
