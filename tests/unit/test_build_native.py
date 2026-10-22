"""scripts/build_native.py: staging, mobile overrides, command and payload checks.

No build, no network, no Flutter: everything runs against temp directories.
"""

from __future__ import annotations

import importlib.util
import io
import json
import tomllib
import zipfile
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

REPO = Path(__file__).resolve().parents[2]


def _load() -> ModuleType:
    spec = importlib.util.spec_from_file_location("build_native", REPO / "scripts/build_native.py")
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    import sys

    sys.modules["build_native"] = module
    spec.loader.exec_module(module)
    return module


bn = _load()


@pytest.fixture(scope="module")
def android_stage(tmp_path_factory: pytest.TempPathFactory) -> Path:
    return bn.stage("android", root=tmp_path_factory.mktemp("native"))  # type: ignore[no-any-return]


# ----------------------------------------------------------------- markers / pins


def test_marker_evaluator_handles_the_lock_forms_and_refuses_others() -> None:
    env = bn.target_environment("android")
    assert bn.marker_true("sys_platform != 'emscripten'", env)
    assert not bn.marker_true("sys_platform == 'win32'", env)
    assert bn.marker_true("implementation_name != 'PyPy'", env)
    assert bn.marker_true("sys_platform == 'win32' or sys_platform == 'android'", env)
    assert not bn.marker_true("sys_platform == 'android' and sys_platform == 'ios'", env)
    with pytest.raises(bn.BuildError):
        bn.marker_true("python_version >= '3.9'", env)


def test_pin_overrides_replace_only_listed_packages_and_report() -> None:
    pins, report = bn.apply_pin_overrides(
        ["cffi==2.1.1", "msgpack==1.2.3", "httpx==0.28.1"],
        {"cffi": "2.0.0", "msgpack": "1.1.2", "absent": "1.0"},
    )
    assert pins == ["cffi==2.0.0", "msgpack==1.1.2", "httpx==0.28.1"]
    assert report["applied"] == {"cffi": "2.1.1 -> 2.0.0", "msgpack": "1.2.3 -> 1.1.2"}
    assert report["not_in_closure"] == {"absent": "1.0"}


def test_requires_python_patch_needs_exactly_one_line() -> None:
    text = 'name = "x"\nrequires-python = ">=3.13,<3.14"\n'
    assert 'requires-python = ">=3.13,<3.15"' in bn.patch_requires_python(text, ">=3.13,<3.15")
    with pytest.raises(bn.BuildError):
        bn.patch_requires_python('name = "x"\n', ">=3.13,<3.15")


def test_overrides_file_is_the_single_source_of_the_mobile_pins() -> None:
    overrides = bn.load_overrides()
    assert overrides["python_version"] == "3.14"
    assert overrides["requires_python"] == ">=3.13,<3.15"
    assert overrides["python_pins"] == {"cffi": "2.0.0", "msgpack": "1.1.2"}
    assert overrides["flutter_overrides"] == {"jni_flutter": "1.0.3"}


# --------------------------------------------------------------------- staging


def test_stage_copies_only_the_allow_listed_layers(android_stage: Path) -> None:
    package = android_stage / "app/relaydeck"
    assert {p.name for p in package.iterdir() if p.name != "__pycache__"} == {
        "__init__.py",
        "client",
        "contracts",
        "domain",
        "lease_diagnostics.py",
        "version.py",
        "_version.py",
    }
    assert (android_stage / "app/main.py").is_file()
    assert (android_stage / "app/assets").is_dir()
    assert not list(android_stage.rglob("serve.py"))
    assert not list(android_stage.rglob("fake_gateway.py"))
    assert not (package / "devtools").exists()
    for server in ("gateway", "connector", "companion"):
        assert not (package / server).exists()
    assert not list(android_stage.rglob(".env*"))


def test_stage_applies_the_mobile_overrides(android_stage: Path) -> None:
    config = tomllib.loads((android_stage / "pyproject.toml").read_text())
    assert config["project"]["requires-python"] == ">=3.13,<3.15"
    deps = config["project"]["dependencies"]
    assert "msgpack==1.1.2" in deps and not any(d.startswith("cffi") for d in deps)
    assert not any(d.startswith("cryptography") for d in deps)
    assert not any(d.startswith(("colorama", "fastapi", "uvicorn", "pytest")) for d in deps)
    overrides = config["tool"]["flet"]["flutter"]["pubspec"]["dependency_overrides"]
    assert overrides == {"jni_flutter": "1.0.3"}
    assert config["tool"]["flet"]["app"] == {"path": "app", "module": "main"}
    for name in ("terminal", "notifications"):
        ext = tomllib.loads((android_stage / f"extensions/{name}/pyproject.toml").read_text())
        assert ext["project"]["requires-python"] == ">=3.13,<3.15"
        assert (android_stage / f"extensions/{name}/src").is_dir()


def test_stage_manifest_records_sources_pins_and_overrides(android_stage: Path) -> None:
    manifest = json.loads((android_stage / "source-manifest.json").read_text())
    assert manifest["platform"] == "android"
    assert manifest["pin_overrides"]["applied"] == {"msgpack": "1.2.3 -> 1.1.2"}
    assert manifest["build_python_version"] == "3.14"
    assert manifest["lock_sha256"] and manifest["root_manifest_sha256"]
    assert "src/relaydeck/client/main.py" in manifest["source_sha256"]
    assert not any("gateway" in key for key in manifest["source_sha256"])


def test_stage_refuses_to_overwrite_without_replace(tmp_path: Path) -> None:
    bn.stage("android", root=tmp_path)
    with pytest.raises(bn.BuildError, match="--replace"):
        bn.stage("android", root=tmp_path)
    assert bn.stage("android", root=tmp_path, replace=True).is_dir()


def test_ios_stage_applies_the_template_patch_through_the_runner(tmp_path: Path) -> None:
    calls: list[list[str]] = []

    def runner(command: list[str], **_: Any) -> Any:
        calls.append(command)
        done = type("Result", (), {})()
        done.returncode = 0
        done.stdout = f"flet template iOS15 materialised: {tmp_path}/tpl\n"
        done.stderr = ""
        return done

    project = bn.stage("ios", root=tmp_path, template_runner=runner)
    assert calls == [[str(REPO / "patches/flet-build-template-ios15/apply.sh")]]
    manifest = json.loads((project / "source-manifest.json").read_text())
    assert manifest["template"] == f"{tmp_path}/tpl"
    deps = tomllib.loads((project / "pyproject.toml").read_text())["project"]["dependencies"]
    assert "msgpack==1.1.2" in deps


def test_native_root_refuses_desktop_and_repository(monkeypatch: pytest.MonkeyPatch) -> None:
    with pytest.raises(bn.BuildError, match="Desktop"):
        bn.native_root({"RD_NATIVE_ROOT": str(Path.home() / "Desktop/stage")})
    with pytest.raises(bn.BuildError, match="outside the repository"):
        bn.native_root({"RD_NATIVE_ROOT": str(REPO / "temp/native")})
    assert bn.native_root({}) == Path(bn.DEFAULT_NATIVE_ROOT).resolve()
    assert bn.native_root({"RD_NATIVE_ROOT": "/private/tmp/x"}) == Path("/private/tmp/x").resolve()


# ------------------------------------------------------------------- the command


def test_android_command_has_the_debug_arm64_flags(tmp_path: Path) -> None:
    command = bn.flet_command("android", tmp_path / "p", tmp_path / "o", "3.14", None)
    assert command[1:3] == ["build", "apk"]
    assert command[command.index("--arch") + 1] == "arm64-v8a"
    assert "--flutter-build-args=--debug" in command
    assert command[command.index("--python-version") + 1] == "3.14"
    assert "--template" not in command and "--yes" not in command


def test_ios_simulator_command_has_no_arch_and_uses_the_patched_template(tmp_path: Path) -> None:
    command = bn.flet_command("ios", tmp_path / "p", tmp_path / "o", "3.14", tmp_path / "tpl")
    assert command[1:3] == ["build", "ios-simulator"]
    assert "--arch" not in command  # all slices are required (blank-screen bug)
    assert command[command.index("--template") + 1] == str(tmp_path / "tpl")
    assert command[command.index("--template-dir") + 1] == "build"
    with pytest.raises(bn.BuildError):
        bn.flet_command("ios", tmp_path / "p", tmp_path / "o", "3.14", None)


def test_build_without_run_prints_and_never_executes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("RD_NATIVE_ROOT", str(tmp_path))
    bn.stage("android", root=tmp_path)

    def explode(*_: Any, **__: Any) -> Any:
        raise AssertionError("a dry run must not execute anything")

    args = bn.parser().parse_args(["build", "--platform", "android"])
    assert bn.build(args, runner=explode) == 0
    out = capsys.readouterr()
    assert "flet" in out.out and "--arch arm64-v8a" in out.out
    assert "dry run" in out.err


def test_build_requires_a_stage_and_rejects_device(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("RD_NATIVE_ROOT", str(tmp_path))
    args = bn.parser().parse_args(["build", "--platform", "android"])
    with pytest.raises(bn.BuildError, match="no stage"):
        bn.build(args)
    device = bn.parser().parse_args(["build", "--platform", "ios", "--device"])
    assert bn.build(device) == 2


def test_toolchain_env_finds_java_and_sdk_from_env(tmp_path: Path) -> None:
    java = tmp_path / "jdk/bin"
    java.mkdir(parents=True)
    (java / "java").write_text("")
    sdk = tmp_path / "sdk/platform-tools"
    sdk.mkdir(parents=True)
    env = {"JAVA_HOME": str(tmp_path / "jdk"), "ANDROID_HOME": str(tmp_path / "sdk")}
    assert bn.toolchain_env("android", env) == env
    assert bn.toolchain_env("ios", env) == {}


# ----------------------------------------------------------------- payload checks


def _app_zip(members: dict[str, bytes]) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        for name, data in members.items():
            archive.writestr(name, data)
    return buffer.getvalue()


def _apk(path: Path, members: dict[str, bytes]) -> Path:
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("assets/flutter_assets/app/app.zip", _app_zip(members))
        archive.writestr("classes.dex", b"dex")
    return path


GOOD = {
    "main.py": b"x",
    "relaydeck/client/app.py": b"x",
    "relaydeck/client/main.cpython-314.pyc": b"x",
    "relaydeck/domain/models.py": b"x",
}


def test_clean_payload_passes_and_gets_sizes_and_hashes(tmp_path: Path) -> None:
    apk = _apk(tmp_path / "app.apk", GOOD)
    manifest = bn.inspect_payload(apk)
    assert manifest["violations"] == []
    assert manifest["artifact"]["sha256"] == bn.sha256_bytes(apk.read_bytes())
    inner = manifest["app_archives"][0]
    assert inner["entries"] == len(GOOD) and inner["has_main"] and inner["sha256"]
    written = bn.write_payload_manifest("android", tmp_path, apk)
    assert json.loads(written.read_text())["platform"] == "android"


@pytest.mark.parametrize(
    "extra",
    [
        "relaydeck/gateway/app.py",
        "relaydeck/devtools/fake_host.py",
        "relaydeck/client/serve.cpython-314.pyc",
        "relaydeck/client/adapters/fake_gateway.py",
        "relaydeck/client/tests/test_x.py",
        ".env",
        "config/.env.production",
        "keys/host.pem",
        "connector.json",
        "id_ed25519",
    ],
)
def test_payload_with_server_dev_test_or_secret_files_fails(tmp_path: Path, extra: str) -> None:
    apk = _apk(tmp_path / "app.apk", {**GOOD, extra: b"x"})
    with pytest.raises(bn.BuildError, match="violations"):
        bn.inspect_payload(apk)


def test_payload_without_an_python_archive_fails(tmp_path: Path) -> None:
    apk = tmp_path / "empty.apk"
    with zipfile.ZipFile(apk, "w") as archive:
        archive.writestr("classes.dex", b"dex")
    with pytest.raises(bn.BuildError, match="app.zip"):
        bn.inspect_payload(apk)


def test_ios_app_directory_is_inspected_the_same_way(tmp_path: Path) -> None:
    app = tmp_path / "Runner.app/Frameworks/App.framework/flutter_assets/app"
    app.mkdir(parents=True)
    (app / "app.zip").write_bytes(_app_zip(GOOD))
    bundle = tmp_path / "Runner.app"
    manifest = bn.inspect_payload(bundle)
    assert manifest["artifact"]["entries"] == 1 and manifest["app_archives"][0]["has_main"]
    (app / "app.zip").write_bytes(_app_zip({**GOOD, "relaydeck/companion/cli.py": b"x"}))
    with pytest.raises(bn.BuildError, match="companion"):
        bn.inspect_payload(bundle)
    (bundle / "secrets.key").write_bytes(b"k")
    (app / "app.zip").write_bytes(_app_zip(GOOD))
    with pytest.raises(bn.BuildError, match="secret-like"):
        bn.inspect_payload(bundle)


def test_find_artifact_picks_apk_or_app(tmp_path: Path) -> None:
    (tmp_path / "apk").mkdir()
    (tmp_path / "apk/app.apk").write_bytes(b"x")
    (tmp_path / "ios/Runner.app").mkdir(parents=True)
    assert bn.find_artifact("android", tmp_path).name == "app.apk"
    assert bn.find_artifact("ios", tmp_path).name == "Runner.app"
    with pytest.raises(bn.BuildError):
        bn.find_artifact("android", tmp_path / "nothing")
