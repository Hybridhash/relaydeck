#!/usr/bin/env python3
"""Reproducible Android / iOS stage + build for RelayDeck (stdlib only).

Native builds cannot use the host project as is: the packaged app must contain
only the client, contracts and domain layers, and mobile packaging needs the
pins recorded in ``patches/native-runtime-pins/mobile-overrides.toml``
(Python 3.14, ``requires-python >=3.13,<3.15``, cffi 2.0.0, msgpack 1.1.2, the
``jni_flutter`` Flutter override). This script bakes those fixes in.

Subcommands (all take ``--platform android|ios``):

``stage``    Copy the allow-listed sources into
             ``${RD_NATIVE_ROOT:-/private/tmp/rd-native}/<platform>/project``,
             generate exact runtime pins from ``uv.lock`` for the target,
             apply the mobile overrides, write ``source-manifest.json`` and
             assert that server and dev code is absent. For iOS it also
             materialises the iOS 15 build template (no network). No build.
``build``    Print the exact ``flet build`` command. ``--run`` executes it and
             then inspects the payload. Without ``--run`` nothing is executed.
``inspect``  Inspect an APK or ``.app`` (default: the build output) and fail
             if server/dev/devtools code, tests, ``.env`` files or keys are in
             it; write ``payload-manifest.json`` (sizes, sha256).

Decisions (see ``patches/native-runtime-pins/README.md``):

* The stage lives outside the repository and outside ``~/Desktop``: the
  antivirus "Safe Files" protection blocks build tools writing there.
* Flutter: plain ``flet build``. Flet resolves ``$HOME/flutter/3.44.8`` and is
  never pointed at a patched SDK: the engine patch in
  ``patches/flutter-ios-pointer-patch`` fixes Flutter *web* on iOS WebKit and
  has no effect on native apps. ``~/flutter`` is read, never edited.
* Template: the iOS 15 template patch is applied for iOS only; Android keeps
  Flet's stock template (the patch changes nothing there).
* iOS simulator: no ``--arch``; all slices are required or Python native
  modules are not bundled (blank screen). ``--device`` is not implemented.
"""

from __future__ import annotations

import argparse
import hashlib
import io
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import tomllib
import zipfile
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
OVERRIDES_FILE = ROOT / "patches/native-runtime-pins/mobile-overrides.toml"
TEMPLATE_APPLY = ROOT / "patches/flet-build-template-ios15/apply.sh"
DEFAULT_NATIVE_ROOT = "/private/tmp/rd-native"
FLUTTER_VERSION = "3.44.8"
PLATFORMS = ("android", "ios")
EXCLUDED = {"serve.py", "__pycache__", "tests", "test", "tool", "tools"}
STAGED_LAYERS = ("client", "contracts", "domain")
FORBIDDEN_PACKAGES = ("gateway", "connector", "companion", "devtools")
FORBIDDEN_MODULES = ("serve", "fake_gateway", "fake_host", "fake_terminal", "scenarios")
SECRET_SUFFIXES = (".pem", ".key", ".p12", ".pfx", ".jks", ".keystore", ".secret")
SECRET_NAMES = (
    "id_rsa",
    "id_ed25519",
    "id_ecdsa",
    "connector.json",
    "companion.toml",
    "cookie.secret",
    "cursor.secret",
)


class BuildError(RuntimeError):
    """A staging, build or payload rule was violated."""


# --------------------------------------------------------------------------- helpers


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def native_root(env: Mapping[str, str] | None = None) -> Path:
    env = os.environ if env is None else env
    root = Path(env.get("RD_NATIVE_ROOT") or DEFAULT_NATIVE_ROOT).expanduser()
    resolved = root.resolve()
    if resolved.is_relative_to(ROOT.resolve()):
        raise BuildError("RD_NATIVE_ROOT must be outside the repository")
    if resolved.is_relative_to(Path.home().resolve() / "Desktop"):
        raise BuildError(
            "RD_NATIVE_ROOT is under ~/Desktop: antivirus Safe Files blocks build tools there"
        )
    return resolved


def normalize(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


# ------------------------------------------------------------ marker evaluation

_COMPARISON = re.compile(r"^\s*(\w+)\s*(==|!=)\s*(['\"])(.*?)\3\s*$")


def target_environment(platform: str) -> dict[str, str]:
    return {
        "sys_platform": platform,
        "platform_system": "Android" if platform == "android" else "iOS",
        "platform_machine": "aarch64" if platform == "android" else "arm64",
        "os_name": "posix",
        "implementation_name": "cpython",
        "platform_python_implementation": "CPython",
    }


def marker_true(marker: str, environment: Mapping[str, str]) -> bool:
    """Evaluate the small PEP 508 subset uv.lock uses; refuse anything else."""
    result = False
    for alternative in re.split(r"\s+or\s+", marker):
        value = True
        for clause in re.split(r"\s+and\s+", alternative):
            match = _COMPARISON.match(clause)
            if not match or match.group(1) not in environment:
                raise BuildError(f"unsupported environment marker: {clause!r}")
            same = environment[match.group(1)] == match.group(4)
            value = value and (same if match.group(2) == "==" else not same)
        result = result or value
    return result


def runtime_closure(
    lock: Mapping[str, Any], project: Mapping[str, Any], platform: str
) -> list[str]:
    """Exact ``name==version`` pins of the runtime closure for the target."""
    packages: dict[str, Any] = {}
    for package in lock["package"]:
        name = normalize(package["name"])
        if name in packages:
            raise BuildError(f"ambiguous lock versions for {name}; resolve explicitly")
        packages[name] = package
    environment = target_environment(platform)
    queue = [
        normalize(re.split(r"[<>=!~;\[ ]", item, maxsplit=1)[0]) for item in project["dependencies"]
    ]
    seen: set[str] = set()
    while queue:
        name = queue.pop()
        if name in seen:
            continue
        if name not in packages:
            raise BuildError(f"{name} is not in uv.lock")
        seen.add(name)
        for dependency in packages[name].get("dependencies", []):
            marker = dependency.get("marker")
            if not marker or marker_true(marker, environment):
                queue.append(normalize(dependency["name"]))
    return [f"{name}=={packages[name]['version']}" for name in sorted(seen)]


# ----------------------------------------------------------------- overrides


def load_overrides(path: Path = OVERRIDES_FILE) -> dict[str, Any]:
    data = tomllib.loads(path.read_text(encoding="utf-8"))
    python = data["python"]
    return {
        "python_version": str(python["build_python_version"]),
        "requires_python": str(python["staged_requires_python"]),
        "python_pins": {normalize(k): str(v) for k, v in python["mobile_overrides"].items()},
        "flutter_overrides": {
            k: str(v) for k, v in data["flutter"]["dependency_overrides"].items()
        },
    }


def apply_pin_overrides(
    pins: Sequence[str], overrides: Mapping[str, str]
) -> tuple[list[str], dict[str, dict[str, str]]]:
    """Replace pinned versions; report what changed and what is not in the closure."""
    out: list[str] = []
    report: dict[str, dict[str, str]] = {"applied": {}, "not_in_closure": {}}
    present: set[str] = set()
    for pin in pins:
        name, _, version = pin.partition("==")
        present.add(name)
        if name in overrides and overrides[name] != version:
            report["applied"][name] = f"{version} -> {overrides[name]}"
            out.append(f"{name}=={overrides[name]}")
        else:
            out.append(pin)
    for name, version in overrides.items():
        if name not in present:
            report["not_in_closure"][name] = version
    return out, report


def patch_requires_python(text: str, requires: str) -> str:
    updated, count = re.subn(
        r'(?m)^requires-python\s*=\s*"[^"]*"\s*$', f'requires-python = "{requires}"', text
    )
    if count != 1:
        raise BuildError("expected exactly one requires-python line")
    return updated


# ----------------------------------------------------------------------- stage


def copy_tree(source: Path, destination: Path, manifest: dict[str, str]) -> None:
    source_root = source.resolve(strict=True)
    for path in sorted(source.rglob("*")):
        relative = path.relative_to(source)
        if any(part.startswith(".") or part in EXCLUDED for part in relative.parts):
            continue
        if path.is_symlink():
            raise BuildError(f"source symlink requires explicit review: {path}")
        if path.is_file():
            if not path.resolve(strict=True).is_relative_to(source_root):
                raise BuildError("source file escapes allow-listed directory")
            target = destination / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            data = path.read_bytes()
            target.write_bytes(data)
            manifest[path.relative_to(ROOT).as_posix()] = sha256_bytes(data)


def copy_file(source: Path, target: Path, manifest: dict[str, str]) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    data = source.read_bytes()
    target.write_bytes(data)
    manifest[source.relative_to(ROOT).as_posix()] = sha256_bytes(data)


def stage_config(pins: Sequence[str], overrides: Mapping[str, Any], version: str) -> str:
    flutter = "".join(
        f"{key} = {json.dumps(value)}\n" for key, value in overrides["flutter_overrides"].items()
    )
    return (
        f'[project]\nname = "relaydeck"\nversion = {json.dumps(version)}\n'
        f'requires-python = "{overrides["requires_python"]}"\n'
        f"dependencies = {json.dumps(list(pins))}\n\n"
        '[tool.flet]\norg = "dev.relaydeck"\nproduct = "RelayDeck"\n'
        'company = "RelayDeck"\nbundle_id = "dev.relaydeck.app"\n\n'
        '[tool.flet.app]\npath = "app"\nmodule = "main"\n\n'
        '[tool.flet.dev_packages]\nrelaydeck-terminal = "extensions/terminal"\n'
        'relaydeck-notifications = "extensions/notifications"\n\n'
        f"[tool.flet.flutter.pubspec.dependency_overrides]\n{flutter}"
    )


def assert_stage_clean(stage: Path) -> None:
    app = stage / "app"
    for package in FORBIDDEN_PACKAGES:
        if (app / "relaydeck" / package).exists():
            raise BuildError(f"server/dev package staged: relaydeck/{package}")
    for module in FORBIDDEN_MODULES:
        found = [p for p in app.rglob(f"{module}.py")]
        if found:
            raise BuildError(f"server/dev module staged: {found[0].relative_to(stage)}")
    for path in stage.rglob("*"):
        if path.is_file() and forbidden_secret(path.name):
            raise BuildError(f"secret-like file staged: {path.relative_to(stage)}")
    if not (app / "main.py").is_file():
        raise BuildError("main.py was not staged")


def materialise_template(
    runner: Callable[..., Any] = subprocess.run, apply_script: Path = TEMPLATE_APPLY
) -> Path:
    """Run the iOS 15 template patch (reads the Flet cache, no network) and return its dir."""
    result = runner(
        [str(apply_script)],
        capture_output=True,
        text=True,
        check=False,
    )
    output = (result.stdout or "") + (result.stderr or "")
    if result.returncode != 0:
        raise BuildError(f"iOS template patch failed: {output.strip()}")
    match = re.search(r"(?:materialised|up to date): (\S+)", output)
    if not match:
        raise BuildError(f"iOS template patch printed no path: {output.strip()}")
    return Path(match.group(1))


def stage(
    platform: str,
    *,
    root: Path | None = None,
    replace: bool = False,
    skip_template: bool = False,
    template_runner: Callable[..., Any] = subprocess.run,
) -> Path:
    base = (root or native_root()) / platform
    project = base / "project"
    if project.is_symlink():
        raise BuildError("stage path is a symlink")
    if project.exists():
        if not replace:
            raise BuildError("stage exists; use --replace only when no build is active")
        if project.name != "project" or not project.resolve().is_relative_to(base.resolve()):
            raise BuildError("refusing to delete an unexpected path")
        shutil.rmtree(project)
    overrides = load_overrides()
    app = project / "app"
    app.mkdir(parents=True)
    manifest_bytes = (ROOT / "pyproject.toml").read_bytes()
    lock_bytes = (ROOT / "uv.lock").read_bytes()
    pins = runtime_closure(
        tomllib.loads(lock_bytes.decode()),
        tomllib.loads(manifest_bytes.decode())["project"],
        platform,
    )
    pins, pin_report = apply_pin_overrides(pins, overrides["python_pins"])
    sources: dict[str, str] = {}
    for layer in STAGED_LAYERS:
        copy_tree(ROOT / "src/relaydeck" / layer, app / "relaydeck" / layer, sources)
    for name in ("__init__.py", "lease_diagnostics.py", "version.py"):
        copy_file(ROOT / "src/relaydeck" / name, app / "relaydeck" / name, sources)
    shutil.copyfile(ROOT / "src/relaydeck/client/main.py", app / "main.py")
    copy_tree(ROOT / "src/relaydeck/client/assets", app / "assets", sources)
    for name in ("terminal", "notifications"):
        extension = ROOT / "extensions" / name
        target = project / "extensions" / name
        copy_tree(extension / "src", target / "src", sources)
        for relative in ("README.md", "LICENSE"):
            if (extension / relative).is_file():
                copy_file(extension / relative, target / relative, sources)
        pyproject = (extension / "pyproject.toml").read_text(encoding="utf-8")
        target.mkdir(parents=True, exist_ok=True)
        (target / "pyproject.toml").write_text(
            patch_requires_python(pyproject, overrides["requires_python"]), encoding="utf-8"
        )
        sources[(extension / "pyproject.toml").relative_to(ROOT).as_posix()] = sha256_bytes(
            (extension / "pyproject.toml").read_bytes()
        )
    app_version = tomllib.loads(manifest_bytes.decode())["project"]["version"]
    (app / "relaydeck" / "_version.py").write_text(f"VERSION = {app_version!r}\n", encoding="utf-8")
    config = stage_config(pins, overrides, app_version)
    tomllib.loads(config)  # must stay valid TOML
    (project / "pyproject.toml").write_text(config, encoding="utf-8")
    template = None
    if platform == "ios" and not skip_template:
        template = str(materialise_template(template_runner))
    evidence = {
        "platform": platform,
        "build_python_version": overrides["python_version"],
        "staged_requires_python": overrides["requires_python"],
        "requirements": pins,
        "pin_overrides": pin_report,
        "flutter_dependency_overrides": overrides["flutter_overrides"],
        "template": template,
        "source_sha256": sources,
        "root_manifest_sha256": sha256_bytes(manifest_bytes),
        "lock_sha256": sha256_bytes(lock_bytes),
        "note": (
            "Exact closure pins for the target; Flet consumes these requirements, not uv.lock. "
            "Target wheels, Dart resolution and the final payload still need build proof."
        ),
    }
    (project / "source-manifest.json").write_text(json.dumps(evidence, indent=2) + "\n")
    assert_stage_clean(project)
    return project


# ----------------------------------------------------------------------- build


def find_dir(env_name: str, candidates: Sequence[str], marker: str, env: Mapping[str, str]) -> str:
    value = env.get(env_name)
    if value and (Path(value).expanduser() / marker).exists():
        return str(Path(value).expanduser())
    for candidate in candidates:
        path = Path(candidate).expanduser()
        if (path / marker).exists():
            return str(path)
    return ""


def toolchain_env(platform: str, env: Mapping[str, str] | None = None) -> dict[str, str]:
    """JAVA_HOME / ANDROID_HOME for Android from the environment, ~/java, ~/Android/sdk."""
    env = os.environ if env is None else env
    if platform != "android":
        return {}
    found = {
        "JAVA_HOME": find_dir("JAVA_HOME", ["~/java", "~/java/Contents/Home"], "bin/java", env),
        "ANDROID_HOME": find_dir("ANDROID_HOME", ["~/Android/sdk"], "platform-tools", env),
    }
    return {key: value for key, value in found.items() if value}


def flet_executable() -> str:
    sibling = Path(sys.executable).parent / "flet"
    return str(sibling) if sibling.exists() else "flet"


def flet_command(
    platform: str,
    project: Path,
    out: Path,
    python_version: str,
    template: Path | None,
    *,
    yes: bool = False,
    extra: Sequence[str] = (),
) -> list[str]:
    if platform == "android":
        command = [
            flet_executable(), "build", "apk", str(project), "-o", str(out),
            "--python-version", python_version,
            "--arch", "arm64-v8a",
            "--flutter-build-args=--debug",
        ]  # fmt: skip
    else:
        if template is None:
            raise BuildError("the iOS 15 template directory is required for iOS")
        command = [
            flet_executable(), "build", "ios-simulator", str(project), "-o", str(out),
            "--python-version", python_version,
            "--template", str(template), "--template-dir", "build",
        ]  # fmt: skip
        # No --arch: all slices are required, otherwise Python native modules
        # are not bundled and the app shows a blank screen.
    if yes:
        command.append("--yes")
    return [*command, *extra]


def format_command(command: Sequence[str], env_extra: Mapping[str, str]) -> str:
    prefix = " ".join(f"{k}={shlex.quote(v)}" for k, v in sorted(env_extra.items()))
    return (prefix + " " if prefix else "") + " ".join(shlex.quote(part) for part in command)


# ---------------------------------------------------------------------- inspect


def forbidden_secret(name: str) -> bool:
    return (
        name == ".env"
        or name.startswith(".env.")
        or name.endswith(SECRET_SUFFIXES)
        or name in SECRET_NAMES
    )


def public_ca_bundle(parts: list[str]) -> bool:
    """certifi's cacert.pem is the public Mozilla CA bundle, not a secret."""
    return parts[-2:] == ["certifi", "cacert.pem"]


def forbidden_member(name: str) -> str:
    """Reason a payload entry must not ship, or an empty string."""
    parts = [part for part in name.split("/") if part]
    if not parts:
        return ""
    base = parts[-1]
    if forbidden_secret(base) and not public_ca_bundle(parts):
        return "secret-like file"
    stem = base.split(".")[0]
    if "relaydeck" in parts:
        after = parts[parts.index("relaydeck") + 1 :]
        if after and after[0] in FORBIDDEN_PACKAGES:
            return f"server/dev package relaydeck/{after[0]}"
        if stem in FORBIDDEN_MODULES:
            return f"server/dev module {stem}"
        if any(part in ("tests", "test") for part in after[:-1]):
            return "test code"
    if any(
        package in parts for package in ("relaydeck_terminal", "relaydeck_notifications")
    ) and any(part in ("tests", "test") for part in parts):
        return "test code"
    return ""


def tree_sha256(files: Mapping[str, str]) -> str:
    digest = hashlib.sha256()
    for relative in sorted(files):
        digest.update(f"{relative}\0{files[relative]}\n".encode())
    return digest.hexdigest()


def _inspect_zip(data: bytes, label: str, violations: list[str]) -> dict[str, Any]:
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        names = [info.filename for info in archive.infolist() if not info.is_dir()]
        for name in names:
            reason = forbidden_member(name)
            if reason:
                violations.append(f"{label}: {name} ({reason})")
        return {"entries": len(names), "has_main": any(n.split(".")[0] == "main" for n in names)}


def inspect_payload(path: Path) -> dict[str, Any]:
    """Inspect an APK (zip) or an ``.app`` directory; raises BuildError on violations."""
    violations: list[str] = []
    archives: list[dict[str, Any]] = []
    if path.is_file():
        data = path.read_bytes()
        artifact: dict[str, Any] = {
            "path": str(path),
            "size": len(data),
            "sha256": sha256_bytes(data),
        }
        with zipfile.ZipFile(path) as apk:
            names = [info.filename for info in apk.infolist() if not info.is_dir()]
            artifact["entries"] = len(names)
            for name in names:
                reason = forbidden_member(name)
                if reason:
                    violations.append(f"{path.name}: {name} ({reason})")
                if name.rsplit("/", 1)[-1] == "app.zip":
                    inner = apk.read(name)
                    archives.append(
                        {"path": name, "size": len(inner), "sha256": sha256_bytes(inner)}
                        | _inspect_zip(inner, name, violations)
                    )
    elif path.is_dir():
        files: dict[str, str] = {}
        size = 0
        for item in sorted(path.rglob("*")):
            if not item.is_file() or item.is_symlink():
                continue
            relative = item.relative_to(path).as_posix()
            data = item.read_bytes()
            files[relative] = sha256_bytes(data)
            size += len(data)
            reason = forbidden_member(relative)
            if reason:
                violations.append(f"{path.name}: {relative} ({reason})")
            if relative.endswith("serious_python_darwin.bundle/app/main.pyc") or relative.endswith(
                "serious_python_darwin.bundle/app/main.py"
            ):
                # iOS (serious_python_darwin) ships the Python app unpacked.
                archives.append(
                    {
                        "path": str(item.parent.relative_to(path)),
                        "size": 0,
                        "sha256": "",
                        "entries": 0,
                    }
                    | {"has_main": True}
                )
            if item.name == "app.zip":
                archives.append(
                    {"path": relative, "size": len(data), "sha256": sha256_bytes(data)}
                    | _inspect_zip(data, relative, violations)
                )
        artifact = {
            "path": str(path),
            "size": size,
            "sha256": tree_sha256(files),
            "entries": len(files),
        }
    else:
        raise BuildError(f"nothing to inspect at {path}")
    if not archives:
        violations.append(
            f"{path.name}: no app.zip or unpacked app/main found; the Python payload is missing"
        )
    elif not any(a["has_main"] for a in archives):
        violations.append(f"{path.name}: app.zip has no main entry point")
    manifest = {"artifact": artifact, "app_archives": archives, "violations": violations}
    if violations:
        raise BuildError("payload violations:\n  " + "\n  ".join(violations))
    return manifest


def find_artifact(platform: str, out: Path) -> Path:
    if platform == "android":
        found = sorted(out.rglob("*.apk"))
    else:
        found = sorted(p for p in out.rglob("*.app") if p.is_dir())
    if not found:
        raise BuildError(f"no {'APK' if platform == 'android' else '.app'} under {out}")
    return found[0]


def write_payload_manifest(platform: str, base: Path, artifact: Path) -> Path:
    manifest = inspect_payload(artifact)
    target = base / "payload-manifest.json"
    target.write_text(json.dumps({"platform": platform} | manifest, indent=2) + "\n")
    return target


# -------------------------------------------------------------------------- cli


def build(args: argparse.Namespace, runner: Callable[..., Any] = subprocess.run) -> int:
    if args.device:
        print(
            "--device is not implemented (signing and provisioning need an owner decision)",
            file=sys.stderr,
        )
        return 2
    base = native_root() / args.platform
    project, out = base / "project", base / "out"
    if not (project / "pyproject.toml").is_file():
        raise BuildError(
            f"no stage at {project}; run: build_native.py stage --platform {args.platform}"
        )
    overrides = load_overrides()
    template: Path | None = None
    if args.platform == "ios":
        recorded = json.loads((project / "source-manifest.json").read_text()).get("template")
        template = Path(recorded) if recorded else None
        if template is None or not template.is_dir():
            raise BuildError("iOS template missing; re-run stage (it applies the iOS 15 patch)")
    env_extra = toolchain_env(args.platform)
    command = flet_command(
        args.platform, project, out, overrides["python_version"], template,
        yes=args.yes, extra=args.extra,
    )  # fmt: skip
    print(format_command(command, env_extra))
    flutter = Path.home() / "flutter" / FLUTTER_VERSION
    if not flutter.is_dir():
        print(
            f"note: {flutter} is missing; Flet will try to install Flutter {FLUTTER_VERSION}",
            file=sys.stderr,
        )
    if args.platform == "android" and len(env_extra) < 2:
        print(
            "note: JAVA_HOME / ANDROID_HOME not found (env, ~/java, ~/Android/sdk)", file=sys.stderr
        )
    if not args.run:
        print("(dry run: pass --run to execute; nothing was built)", file=sys.stderr)
        return 0
    result = runner(command, env={**os.environ, **env_extra}, check=False)
    if result.returncode != 0:
        return int(result.returncode)
    artifact = find_artifact(args.platform, out)
    print(f"payload manifest: {write_payload_manifest(args.platform, base, artifact)}")
    return 0


def parser() -> argparse.ArgumentParser:
    top = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    sub = top.add_subparsers(dest="command", required=True)
    for name in ("stage", "build", "inspect"):
        item = sub.add_parser(name)
        item.add_argument("--platform", choices=PLATFORMS, required=True)
        if name == "stage":
            item.add_argument("--replace", action="store_true")
            item.add_argument("--skip-template", action="store_true")
        elif name == "build":
            item.add_argument("--run", action="store_true", help="execute the printed command")
            item.add_argument("--device", action="store_true", help="not implemented")
            item.add_argument("--yes", action="store_true", help="let Flet install missing tools")
            item.add_argument("extra", nargs="*", help="extra flet arguments, after --")
        else:
            item.add_argument("path", nargs="?", type=Path)
    return top


def main(argv: Sequence[str] | None = None) -> int:
    args = parser().parse_args(argv)
    try:
        if args.command == "stage":
            project = stage(args.platform, replace=args.replace, skip_template=args.skip_template)
            print(f"staged {project}")
            return 0
        if args.command == "build":
            return build(args)
        base = native_root() / args.platform
        artifact = args.path or find_artifact(args.platform, base / "out")
        print(f"payload manifest: {write_payload_manifest(args.platform, base, artifact)}")
        return 0
    except BuildError as error:
        print(f"error: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
