"""Bundle store containment, the installed hook's depth/deadline fallbacks, repair snapshots.

Temporary directories only; nothing outside them is read or written.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

import pytest

from relaydeck import webbundle

HOOK = Path(__file__).parents[2] / "scripts/statusline_limits_hook.py"
GOOD_JS = (
    '"touchend";' * 4
    + '"touchcancel";' * 2
    + '"abort";' * 3
    + '"flet_reconnect_merge_rd1";'
    + "var app = 1;"
)


def _bundle(root: Path, manifest: dict[str, Any] | None = None) -> Path:
    out = root / "out"
    out.mkdir(parents=True)
    (out / "main.dart.js").write_text(GOOD_JS, encoding="utf-8")
    (out / "assets").mkdir()
    (out / "assets" / "font.bin").write_bytes(b"\x00\x01")
    if manifest is not None:
        (root / "out.build-manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    return out


def _store(tmp_path: Path) -> Path:
    store = tmp_path / "store"
    store.mkdir(mode=0o700)
    return store


@pytest.mark.parametrize(
    "revision",
    ["../../escape", "/etc/outside", "a/b", "..", "x" * 300, "zzzzzzzzzzzz", "A" * 40, ""],
)
def test_a_hostile_revision_never_becomes_a_path(tmp_path: Path, revision: str) -> None:
    source = _bundle(tmp_path / "src", {"app": {"revision": revision}})
    store = _store(tmp_path)
    record = webbundle.install(source, root=store)
    assert webbundle.BUILD_ID.fullmatch(record.build_id)
    assert record.path.parent == store
    assert not (tmp_path / "escape").exists()


def test_a_valid_revision_is_kept_bounded(tmp_path: Path) -> None:
    source = _bundle(tmp_path / "src", {"app": {"revision": "a" * 40}})
    record = webbundle.install(source, root=_store(tmp_path))
    assert record.build_id.startswith("a" * 12 + "-")


def test_a_symlink_in_the_source_tree_is_refused_and_nothing_is_left(tmp_path: Path) -> None:
    private = tmp_path / "private.txt"
    private.write_text("synthetic-private-content")
    source = _bundle(tmp_path / "src")
    (source / "assets" / "link.txt").symlink_to(private)
    store = _store(tmp_path)
    with pytest.raises(webbundle.BundleError, match="link or special file"):
        webbundle.install(source, root=store)
    assert list(store.iterdir()) == []


def test_a_symlinked_main_script_is_refused(tmp_path: Path) -> None:
    source = _bundle(tmp_path / "src")
    real = tmp_path / "real.js"
    real.write_text(GOOD_JS)
    (source / "main.dart.js").unlink()
    (source / "main.dart.js").symlink_to(real)
    with pytest.raises(webbundle.BundleError):
        webbundle.install(source, root=_store(tmp_path))


def test_a_special_file_in_the_source_tree_is_refused(tmp_path: Path) -> None:
    source = _bundle(tmp_path / "src")
    os.mkfifo(source / "assets" / "pipe")
    store = _store(tmp_path)
    with pytest.raises(webbundle.BundleError, match="special file"):
        webbundle.install(source, root=store)
    assert list(store.iterdir()) == []


def test_a_writable_store_is_refused(tmp_path: Path) -> None:
    store = tmp_path / "shared"
    store.mkdir()
    store.chmod(0o777)
    with pytest.raises(webbundle.BundleError, match="private directory"):
        webbundle.install(_bundle(tmp_path / "src"), root=store)


def test_a_symlinked_destination_is_not_followed(tmp_path: Path) -> None:
    source = _bundle(tmp_path / "src")
    store = _store(tmp_path)
    first = webbundle.install(source, root=store)
    target = tmp_path / "elsewhere"
    target.mkdir()
    link = store / first.build_id
    shutil.rmtree(link)
    link.symlink_to(target)
    with pytest.raises(webbundle.BundleError):
        webbundle.install(source, root=store)
    assert list(target.iterdir()) == []


def test_another_install_s_staging_directory_is_left_alone(tmp_path: Path) -> None:
    store = _store(tmp_path)
    foreign = store / ".staging-someone-else"
    foreign.mkdir()
    (foreign / "keep").write_text("x")
    webbundle.install(_bundle(tmp_path / "src"), root=store)
    assert (foreign / "keep").read_text() == "x"
    assert [p.name for p in store.iterdir() if p.name.startswith(".staging")] == [foreign.name]


def test_symlinked_children_are_not_listed_as_bundles(tmp_path: Path) -> None:
    store = _store(tmp_path)
    record = webbundle.install(_bundle(tmp_path / "src"), root=store)
    (store / "alias").symlink_to(record.path)
    assert [row["build_id"] for row in webbundle.list_bundles(store)] == [record.build_id]


# -- the installed hook ---------------------------------------------------------------


def _run(cache: Path, stdin: bytes, timeout: float = 8) -> subprocess.CompletedProcess[bytes]:
    return subprocess.run(
        [sys.executable, "-B", str(HOOK), "--cache-path", str(cache)],
        input=stdin,
        capture_output=True,
        timeout=timeout,
    )


def test_deeply_nested_input_gets_the_fallback_line(tmp_path: Path) -> None:
    nested = b"[" * 30_000 + b"]" * 30_000
    result = _run(tmp_path / "state/cache.json", nested)
    assert result.returncode == 0 and not result.stderr
    assert result.stdout.decode().strip().endswith("ctx ?")
    assert not (tmp_path / "state/cache.json").exists()


def test_nesting_just_over_the_limit_is_refused_and_under_is_parsed(tmp_path: Path) -> None:
    deep = json.dumps({"a": [[[[]]]]}).encode()
    assert _run(tmp_path / "s/c.json", deep).returncode == 0
    over = b'{"x":' * 40 + b"1" + b"}" * 40
    result = _run(tmp_path / "s/c.json", over)
    assert result.returncode == 0 and result.stdout.decode().strip().endswith("ctx ?")


def test_a_deeply_nested_cache_file_is_not_a_crash(tmp_path: Path) -> None:
    import importlib.util

    resource = Path(__file__).parents[2] / "src/relaydeck/usage/resources/statusline_limits_hook.py"
    spec = importlib.util.spec_from_file_location("usage_hook_deep", resource)
    assert spec and spec.loader
    hook = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(hook)
    cache = tmp_path / "state/cache.json"
    cache.parent.mkdir(mode=0o700)
    cache.write_bytes(b"[" * 20_000 + b"]" * 20_000)
    cache.chmod(0o600)
    with pytest.raises(ValueError):
        hook.read_cache(cache)
    windows = {"five_hour": {"used_percentage": 10, "resets_at": 2_000_000_000}}
    assert hook.persist(cache, windows, 1000) in (True, False)  # never raises


def test_a_stdin_that_never_closes_ends_at_the_deadline(tmp_path: Path) -> None:
    process = subprocess.Popen(
        [sys.executable, "-B", str(HOOK), "--cache-path", str(tmp_path / "s/c.json")],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    started = time.monotonic()
    try:
        process.stdin.write(b'{"model":')  # type: ignore[union-attr]
        process.stdin.flush()  # type: ignore[union-attr]
        deadline = started + 6
        while process.poll() is None and time.monotonic() < deadline:
            time.sleep(0.05)
        assert process.poll() == 0, "the hook must exit on its own"
        assert time.monotonic() - started < 5
        out = process.stdout.read()  # type: ignore[union-attr]
        assert out.decode().strip().endswith("ctx ?")
    finally:
        if process.poll() is None:
            process.kill()
        process.wait()


# -- repair: one snapshot ----------------------------------------------------------------


def test_a_file_that_keeps_changing_is_neither_classified_nor_bound(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from relaydeck.usage import hook_repair

    settings = tmp_path / "settings.json"
    settings.write_text("{}")
    settings.chmod(0o600)

    class Source:
        kind = "tool-a"
        format = "f"
        limits_format = "json-statusline-limits-v1"
        limits_settings = settings
        limits_cache = tmp_path / "cache.json"

    counter = iter(range(1000))
    monkeypatch.setattr(
        hook_repair, "_settings_image", lambda _s: json.dumps({"n": next(counter)}).encode()
    )
    diagnosis = hook_repair.diagnose_source(Source(), hooks_dir=tmp_path / "hooks")
    assert diagnosis.repairable is False
    assert diagnosis.reason == "settings_unreadable"


def _swap_when_listed(monkeypatch: pytest.MonkeyPatch, name: str, replace: Any) -> list[int]:
    """Run `replace()` once, right after the copier has checked the entry called `name`."""
    real_stat = os.stat
    done: list[int] = []

    def stat_then_swap(path: Any, *args: Any, **kwargs: Any) -> Any:
        result = real_stat(path, *args, **kwargs)
        if path == name and kwargs.get("dir_fd") is not None and not done:
            done.append(1)
            replace()
        return result

    monkeypatch.setattr(os, "stat", stat_then_swap)
    return done


def test_a_directory_replaced_by_a_symlink_after_the_check_imports_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    secret = tmp_path / "outside"
    secret.mkdir()
    (secret / "private.txt").write_text("synthetic-private-content")
    source = _bundle(tmp_path / "src")

    def replace() -> None:
        shutil.rmtree(source / "assets")
        (source / "assets").symlink_to(secret)

    done = _swap_when_listed(monkeypatch, "assets", replace)
    store = _store(tmp_path)
    with pytest.raises(webbundle.BundleError, match="changed while"):
        webbundle.install(source, root=store)
    assert done and list(store.iterdir()) == []


def test_a_directory_replaced_by_another_real_directory_is_rejected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    other = tmp_path / "other"
    other.mkdir()
    (other / "private.txt").write_text("synthetic-private-content")
    source = _bundle(tmp_path / "src")

    def replace() -> None:
        shutil.rmtree(source / "assets")
        shutil.copytree(other, source / "assets")

    done = _swap_when_listed(monkeypatch, "assets", replace)
    store = _store(tmp_path)
    with pytest.raises(webbundle.BundleError, match="changed while"):
        webbundle.install(source, root=store)
    assert done and list(store.iterdir()) == []


def test_a_file_replaced_by_a_symlink_after_the_check_is_not_followed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    private = tmp_path / "private.txt"
    private.write_text("synthetic-private-content")
    source = _bundle(tmp_path / "src")

    def replace() -> None:
        (source / "assets" / "font.bin").unlink()
        (source / "assets" / "font.bin").symlink_to(private)

    done = _swap_when_listed(monkeypatch, "font.bin", replace)
    store = _store(tmp_path)
    with pytest.raises(webbundle.BundleError):
        webbundle.install(source, root=store)
    assert done and list(store.iterdir()) == []


def test_the_size_limit_counts_the_bytes_actually_copied(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = _bundle(tmp_path / "src")
    # The listed size is small; the budget is enforced on what is read.
    monkeypatch.setattr(webbundle, "MAX_BYTES", 8)
    store = _store(tmp_path)
    with pytest.raises(webbundle.BundleError, match="too large"):
        webbundle.install(source, root=store)
    assert list(store.iterdir()) == []


def test_a_store_that_is_a_symlink_to_a_writable_directory_is_refused(tmp_path: Path) -> None:
    real = tmp_path / "real"
    real.mkdir()
    real.chmod(0o777)
    link = tmp_path / "store-link"
    link.symlink_to(real)
    with pytest.raises(webbundle.BundleError, match="private directory"):
        webbundle.install(_bundle(tmp_path / "src"), root=link)
