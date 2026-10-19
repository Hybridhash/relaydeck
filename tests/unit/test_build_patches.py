"""Q2: patch folders, apply scripts and the build-manifest gate (no builds)."""

from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import shutil
import subprocess
import sys
import tomllib
from pathlib import Path
from types import ModuleType

import pytest

ROOT = Path(__file__).resolve().parents[2]
PATCHES = ROOT / "patches"
FOLDERS = (
    "flutter-file-upload-patch",
    "flutter-ios-pointer-patch",
    "flet-build-template-ios15",
    "native-runtime-pins",
    "flet-reconnect-merge-patch",
)


def _manifest() -> ModuleType:
    spec = importlib.util.spec_from_file_location(
        "rd_build_manifest", ROOT / "scripts" / "build_manifest.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _run(*argv: str, **env: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        list(argv),
        capture_output=True,
        text=True,
        check=False,
        env={**os.environ, **env},
        timeout=120,
    )


def test_one_readme_documents_every_patch_with_base_and_removal() -> None:
    lowered = (PATCHES / "README.md").read_text(encoding="utf-8").lower()
    for folder in FOLDERS:
        assert f"`{folder}/`" in lowered, folder
        assert not (PATCHES / folder / "README.md").exists(), folder  # one concise guide only
    assert "remove" in lowered and "base" in lowered
    assert "no git" not in lowered  # a patch README is not a work log


@pytest.mark.parametrize("folder", (*FOLDERS[:3], "flet-reconnect-merge-patch"))
def test_apply_scripts_are_executable_and_parse(folder: str) -> None:
    script = PATCHES / folder / "apply.sh"
    assert os.access(script, os.X_OK)
    assert _run("bash", "-n", str(script)).returncode == 0


def test_build_script_parses_and_patch_blocks_are_delimited() -> None:
    script = ROOT / "scripts" / "build_web_client.sh"
    assert _run("bash", "-n", str(script)).returncode == 0
    text = script.read_text(encoding="utf-8")
    for name in (*FOLDERS[:2], "flet-reconnect-merge-patch"):
        assert f"# >>> patch: {name}" in text
        assert f"# <<< patch: {name}" in text
    assert "--check-markers" in text and "RD_BUILD_DRY_RUN" in text


def test_pointer_patch_pins_upstream_hash_and_lists_five_files() -> None:
    folder = PATCHES / "flutter-ios-pointer-patch"
    digest = hashlib.sha256((folder / "upstream-pr189608.diff").read_bytes()).hexdigest()
    assert digest == "5061ae31d5e6c14c1b9ba6490a4502f51de11f327afd82813f5c20e61d12a00f"
    rows = [
        line.split()
        for line in (folder / "expected-sha256.txt").read_text().splitlines()
        if line.strip() and not line.startswith("#")
    ]
    assert len(rows) == 5 and all(len(row) == 3 and row[0] != row[1] for row in rows)
    touched = {
        line[6:]
        for name in (
            "engine-pr189608.diff",
            "flutter-web-sdk-lib.diff",
            "flutter-tools-web-libs.diff",
        )
        for line in (folder / name).read_text().splitlines()
        if line.startswith("+++ b/")
    }
    assert touched == {row[2] for row in rows}


def test_mobile_overrides_record_the_native_pins() -> None:
    data = tomllib.loads((PATCHES / "native-runtime-pins" / "mobile-overrides.toml").read_text())
    assert data["python"]["build_python_version"] == "3.14"
    assert data["python"]["staged_requires_python"] == ">=3.13,<3.15"
    assert data["python"]["mobile_overrides"] == {"cffi": "2.0.0", "msgpack": "1.1.2"}
    assert data["flutter"]["dependency_overrides"]["jni_flutter"] == "1.0.3"
    assert data["build_environment"]["stage_root"] == "/private/tmp/rd-native"


def test_root_pyproject_and_lock_are_untouched_by_the_pins() -> None:
    text = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    assert 'requires-python = ">=3.13,<3.14"' in text


PUB = Path(os.environ.get("PUB_CACHE", Path.home() / ".pub-cache"))
PRISTINE_PICKER = PUB / "hosted/pub.dev/file_picker-11.0.3"


@pytest.mark.skipif(not PRISTINE_PICKER.is_dir(), reason="pristine file_picker 11.0.3 not cached")
def test_file_picker_diff_reproduces_the_vendored_tree(tmp_path: Path) -> None:
    script = PATCHES / "flutter-file-upload-patch" / "apply.sh"
    dest = tmp_path / "pkg"
    first = _run(str(script), str(dest))
    assert first.returncode == 0, first.stderr
    vendored = ROOT / "extensions/terminal/third_party/file_picker-11.0.3-rd1"
    assert subprocess.run(["diff", "-r", str(dest), str(vendored)], check=False).returncode == 0
    assert _run(str(script), "--check", str(dest)).returncode == 0
    (dest / "LICENSE").write_text("tampered")
    assert _run(str(script), "--check", str(dest)).returncode == 1
    assert _run(str(script), str(dest)).returncode == 1  # refuses without --force
    assert _run(str(script), "--force", str(dest)).returncode == 0
    assert _run(str(script), "--check", str(dest)).returncode == 0


PRISTINE_FLET = PUB / "hosted/pub.dev/flet-1.0.3"


def _file_tree(root: Path) -> dict[Path, bytes | str]:
    """Compare every file/link; empty local directories are not source drift."""
    return {
        p.relative_to(root): os.readlink(p) if p.is_symlink() else p.read_bytes()
        for p in root.rglob("*")
        if p.is_symlink() or p.is_file()
    }


@pytest.mark.skipif(not PRISTINE_FLET.is_dir(), reason="pristine flet 1.0.3 not cached")
def test_flet_merge_diff_reproduces_tree_and_rejects_drift(tmp_path: Path) -> None:
    script = PATCHES / "flet-reconnect-merge-patch" / "apply.sh"
    dest = tmp_path / "pkg"
    first = _run(str(script), str(dest))
    assert first.returncode == 0, first.stderr
    vendored = ROOT / "extensions/terminal/third_party/flet-1.0.3-rd1"
    (dest / "empty" / "nested").mkdir(parents=True)
    assert _file_tree(dest) == _file_tree(vendored)
    (dest / "empty" / "nested").rmdir()
    (dest / "empty").rmdir()
    assert _run(str(script), "--check", str(dest)).returncode == 0
    model = dest / "lib/src/models/control.dart"
    model.write_text(model.read_text().replace("flet_reconnect_merge_rd1", "tampered"))
    assert _file_tree(dest) != _file_tree(vendored)
    assert _run(str(script), "--check", str(dest)).returncode == 1
    assert _run(str(script), str(dest)).returncode == 1
    assert _run(str(script), "--force", str(dest)).returncode == 0
    assert _run(str(script), "--check", str(dest)).returncode == 0


def test_flet_and_picker_overrides_resolve_from_generated_pubspec() -> None:
    viewer = ROOT / "extensions/terminal/examples/viewer"
    data = tomllib.loads((viewer / "pyproject.toml").read_text())
    overrides = data["tool"]["flet"]["flutter"]["pubspec"]["dependency_overrides"]
    for name, folder in (
        ("flet", "flet-1.0.3-rd1"),
        ("file_picker", "file_picker-11.0.3-rd1"),
    ):
        dest = (viewer / "build/flutter" / overrides[name]["path"]).resolve()
        assert dest == ROOT / "extensions/terminal/third_party" / folder
        assert (dest / "pubspec.yaml").is_file()


TEMPLATE_ZIP = Path.home() / ".flet/cache/build-template/v1.0.3/flet-build-template.zip"
TEMPLATE_SHA = "b77a99b6f897dc716027200573b97721b08df04a22b27fcbc1681d957e0428df"


@pytest.mark.skipif(
    not TEMPLATE_ZIP.is_file()
    or hashlib.sha256(TEMPLATE_ZIP.read_bytes()).hexdigest() != TEMPLATE_SHA,
    reason="pinned Flet 1.0.3 template zip not cached",
)
def test_template_patch_sets_ios_15_everywhere(tmp_path: Path) -> None:
    dest = tmp_path / "template"
    result = _run(str(PATCHES / "flet-build-template-ios15" / "apply.sh"), str(dest))
    assert result.returncode == 0, result.stderr
    ios = dest / "build" / "{{cookiecutter.out_dir}}" / "ios"
    podfile = (ios / "Podfile").read_text()
    pbx = (ios / "Runner.xcodeproj" / "project.pbxproj").read_text()
    assert "platform :ios, '15.0'" in podfile and "'13.0'" not in podfile
    assert "IPHONEOS_DEPLOYMENT_TARGET'] = '15.0'" in podfile
    assert "IPHONEOS_DEPLOYMENT_TARGET = 13.0" not in pbx
    assert pbx.count("IPHONEOS_DEPLOYMENT_TARGET = 15.0;") == 3


PRISTINE_SDK = Path.home() / "flutter" / "3.44.8"


@pytest.mark.skipif(
    os.environ.get("RD_TEST_SDK_CLONE") != "1" or not PRISTINE_SDK.is_dir(),
    reason="opt-in (RD_TEST_SDK_CLONE=1): clones the 3.8G SDK into tmp",
)
def test_sdk_patch_builds_an_isolated_copy_and_leaves_the_source_alone(tmp_path: Path) -> None:
    web = PRISTINE_SDK / "packages/flutter_tools/lib/src/build_system/targets/web.dart"
    before = hashlib.sha256(web.read_bytes()).hexdigest()
    dest = tmp_path / "sdk"
    result = _run(str(PATCHES / "flutter-ios-pointer-patch" / "apply.sh"), str(dest))
    assert result.returncode == 0, result.stderr
    assert hashlib.sha256(web.read_bytes()).hexdigest() == before
    assert (tmp_path / "home" / "flutter" / "3.44.8").is_symlink()
    ensure = _run(str(PATCHES / "flutter-ios-pointer-patch" / "apply.sh"), "--ensure", str(dest))
    assert "up to date" in ensure.stdout
    shutil.rmtree(dest)


# ---- scripts/build_manifest.py -------------------------------------------------


def test_repair_markers_use_calibrated_counts() -> None:
    module = _manifest()
    patched = (
        '"touchend" ' * 4
        + '"touchcancel" ' * 2
        + '"abort" ' * 3
        + '"flet_reconnect_merge_rd1" "flet_reconnect_wake_rd1"'
    )
    pristine = '"touchend" ' * 3 + '"touchcancel" ' + '"abort"'
    assert module.markers_ok(module.repair_markers(patched))
    assert not module.markers_ok(module.repair_markers(pristine))
    only = module.repair_markers('"abort" ' * 3, ("flutter-file-upload-patch",))
    assert list(only) == ["flutter-file-upload-patch"] and module.markers_ok(only)


def test_check_markers_cli_gates_the_bundle(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    module = _manifest()
    good = tmp_path / "good.js"
    good.write_text(
        '"touchend" ' * 4
        + '"touchcancel" ' * 2
        + '"abort" ' * 3
        + '"flet_reconnect_merge_rd1" "flet_reconnect_wake_rd1"'
    )
    bad = tmp_path / "bad.js"
    bad.write_text('"touchend" "touchcancel" "abort"')
    assert module.main(["--check-markers", str(good)]) == 0
    assert module.main(["--check-markers", str(bad)]) == 1
    assert module.main(["--check-markers", str(bad), "--patches", "flutter-file-upload-patch"]) == 1
    assert (
        module.main(["--check-markers", str(good), "--patches", "flet-reconnect-merge-patch"]) == 0
    )
    assert (
        module.main(["--check-markers", str(bad), "--patches", "flet-reconnect-merge-patch"]) == 1
    )
    assert module.main(["--check-markers", str(good), "--patches", "no-such-patch"]) == 1
    assert module.main(["--check-markers", str(tmp_path / "missing.js")]) == 1
    capsys.readouterr()


def test_manifest_records_patches_sdk_lockfiles_and_output(tmp_path: Path) -> None:
    module = _manifest()
    patch_root = tmp_path / "repo"
    (patch_root / "patches" / "demo").mkdir(parents=True)
    (patch_root / "patches" / "demo" / "a.diff").write_text("x")
    (patch_root / "uv.lock").write_text("lock")
    sdk = tmp_path / "sdk"
    (sdk / "bin/cache").mkdir(parents=True)
    (sdk / "bin/cache/flutter.version.json").write_text(
        json.dumps({"flutterVersion": "3.44.8", "frameworkRevision": "058e0af2c2"})
    )
    out = tmp_path / "out"
    (out / "assets" / "fonts" / "f").mkdir(parents=True)
    (out / "assets/fonts/f/a.ttf").write_bytes(b"font")
    (out / "main.dart.js").write_text('"abort" "abort" "abort"')
    manifest = module.build_manifest(
        patch_root,
        aab=None,
        apk=None,
        android_manifest=None,
        template_rev=None,
        packaged_python=None,
        flutter_sdk=str(sdk),
        web_out=str(out),
    )
    assert manifest["patches"]["demo"]["count"] == 1
    assert "uv.lock" in manifest["lockfiles"]
    assert manifest["flutter_sdk"]["framework_revision"] == "058e0af2c2"
    assert manifest["flutter_sdk"]["pointer_patch"]["status"] == "unavailable"
    web = manifest["web_output"]
    assert web["fonts"]["count"] == 1 and web["file_count"] == 2
    assert web["repair_markers"]["flutter-file-upload-patch"]["ok"] is True
    assert web["repair_markers"]["flutter-ios-pointer-patch"]["ok"] is False


def test_sdk_record_distinguishes_pristine_and_patched(tmp_path: Path) -> None:
    module = _manifest()
    repo = tmp_path / "repo"
    expected = repo / "patches/flutter-ios-pointer-patch"
    expected.mkdir(parents=True)
    old, new = b"old", b"new"
    row = f"{hashlib.sha256(old).hexdigest()}  {hashlib.sha256(new).hexdigest()}  lib/x.dart"
    (expected / "expected-sha256.txt").write_text(f"# header\n{row}\n")
    sdk = tmp_path / "sdk"
    (sdk / "bin/cache").mkdir(parents=True)
    (sdk / "bin/cache/flutter.version.json").write_text("{}")
    (sdk / "lib").mkdir()
    (sdk / "lib/x.dart").write_bytes(old)
    assert module.flutter_sdk_record(str(sdk), repo)["pointer_patch"]["patched"] is False
    (sdk / "lib/x.dart").write_bytes(new)
    assert module.flutter_sdk_record(str(sdk), repo)["pointer_patch"]["patched"] is True
    assert module.flutter_sdk_record(None, repo)["status"] == "unavailable"
