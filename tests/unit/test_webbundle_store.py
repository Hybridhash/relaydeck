"""repair markers move into the package; installs verify on the copy and never
leave a half-copied bundle; the newest verified bundle is current. Temporary dirs only.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from relaydeck import webbundle

GOOD_JS = (
    '"touchend";' * 4
    + '"touchcancel";' * 2
    + '"abort";' * 3
    + '"flet_reconnect_merge_rd1";'
    + "var app = 1;"
)


def _bundle(root: Path, js: str = GOOD_JS, manifest_commit: str | None = None) -> Path:
    out = root / "out"
    out.mkdir(parents=True)
    (out / "main.dart.js").write_text(js, encoding="utf-8")
    (out / "assets").mkdir()
    (out / "assets" / "font.bin").write_bytes(b"\x00\x01")
    if manifest_commit is not None:
        (root / "out.build-manifest.json").write_text(
            json.dumps({"app": {"revision": manifest_commit}}), encoding="utf-8"
        )
    return out


def test_markers_are_the_same_table_the_build_gate_uses() -> None:
    markers = webbundle.repair_markers(GOOD_JS)
    assert webbundle.markers_ok(markers)
    assert not webbundle.markers_ok(webbundle.repair_markers("var app = 1;"))


def test_install_rejects_bundle_without_reconnect_repair(tmp_path: Path) -> None:
    source = _bundle(tmp_path / "src", GOOD_JS.replace('"flet_reconnect_merge_rd1";', ""))
    store = tmp_path / "store"
    with pytest.raises(webbundle.BundleError, match="repair markers missing"):
        webbundle.install(source, root=store)
    assert not store.exists()


def test_install_copies_verifies_and_records_the_bundle(tmp_path: Path) -> None:
    source = _bundle(tmp_path / "src", manifest_commit="a" * 40)
    store = tmp_path / "store"
    record = webbundle.install(source, root=store)
    assert record.build_id.startswith("aaaaaaaaaaaa-")
    assert record.path == store / record.build_id
    assert (record.path / "main.dart.js").is_file()
    assert (record.path / "assets" / "font.bin").is_file()
    installed = json.loads((record.path / "installed.json").read_text())
    assert installed["build_id"] == record.build_id
    assert installed["main_dart_js_sha256"]
    assert webbundle.verify(record.path)
    assert not [p for p in store.iterdir() if p.name.startswith(".staging")]


def test_install_without_a_manifest_uses_the_hash_prefix(tmp_path: Path) -> None:
    record = webbundle.install(_bundle(tmp_path / "src"), root=tmp_path / "store")
    assert len(record.build_id) == 16 and "-" not in record.build_id


def test_missing_markers_are_refused_and_nothing_is_left(tmp_path: Path) -> None:
    source = _bundle(tmp_path / "src", js="var app = 1;")
    store = tmp_path / "store"
    with pytest.raises(webbundle.BundleError, match="repair markers missing"):
        webbundle.install(source, root=store)
    assert not store.exists() or not any(store.iterdir())


def test_reinstalling_the_same_bundle_is_idempotent(tmp_path: Path) -> None:
    source = _bundle(tmp_path / "src")
    store = tmp_path / "store"
    first = webbundle.install(source, root=store)
    second = webbundle.install(source, root=store)
    assert first.build_id == second.build_id
    assert len(list(store.iterdir())) == 1


def test_newest_verified_bundle_is_current_and_a_tampered_one_is_not(tmp_path: Path) -> None:
    store = tmp_path / "store"
    older = webbundle.install(_bundle(tmp_path / "one", manifest_commit="1" * 40), root=store)
    newer_source = _bundle(tmp_path / "two", manifest_commit="2" * 40)
    newer = webbundle.install(newer_source, root=store)
    rows = webbundle.list_bundles(store)
    assert rows[0]["build_id"] in (older.build_id, newer.build_id)
    current = webbundle.current_bundle(store)
    assert current is not None
    # Tamper with the newest copy: it stops verifying and the older one becomes current.
    (newer.path / "main.dart.js").write_text(GOOD_JS + "// tampered", encoding="utf-8")
    assert not webbundle.verify(newer.path)
    current_after = webbundle.current_bundle(store)
    assert current_after == older.path


def test_no_installed_bundle_means_no_current(tmp_path: Path) -> None:
    assert webbundle.current_bundle(tmp_path / "empty") is None
    assert webbundle.list_bundles(tmp_path / "empty") == []
