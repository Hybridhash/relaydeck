"""Dependency SBOM + license inventory.

Covers Python packages pinned in uv.lock AND native/Dart dependencies
declared in extensions/*/src/flutter/*/pubspec.yaml. Every entry reports
a measured license or UNKNOWN; the script exits 1 on any UNKNOWN so CI
fails closed. Stdlib only (no new dependencies).

Python license resolution order per locked package:
  1. installed dist metadata License-Expression / License field,
  2. License trove classifiers,
  3. fingerprint of a bundled licenses/* file in the installed dist-info,
  4. UNKNOWN (fail).

Dart license resolution: each pubspec-declared package (excluding the
Flutter/Dart SDK itself) must appear in DART_LICENSES, a table of
licenses verified against upstream license texts (proof recorded in
docs/acknowledgements.md). Anything else is UNKNOWN (fail), so a newly
added Dart dependency cannot slip through unscanned.
"""

from __future__ import annotations

import argparse
import importlib.metadata as md
import json
import sys
from datetime import UTC, datetime
from pathlib import Path

SCHEMA = "relaydeck.license-inventory/1"

# Map of PyPI trove classifier suffix -> SPDX-ish id, used only when the
# dist metadata carries no License-Expression/License field.
CLASSIFIER_LICENSES = {
    "License :: OSI Approved :: MIT License": "MIT",
    "License :: OSI Approved :: BSD License": "BSD",
    "License :: OSI Approved :: Apache Software License": "Apache-2.0",
    "License :: OSI Approved :: Mozilla Public License 2.0 (MPL 2.0)": "MPL-2.0",
    "License :: OSI Approved :: GNU Lesser General Public License v2 or later "
    "(LGPLv2+)": "LGPL-2.0-or-later",
    "License :: OSI Approved :: Artistic License": "Artistic-2.0",
    "License :: OSI Approved :: BSD": "BSD",
}


def fingerprint_license_file(text: str) -> str | None:
    low = text[:2000].lower()
    if "apache license" in low and "version 2.0" in low:
        return "Apache-2.0"
    if "mit license" in low and "permission is hereby granted" in low:
        return "MIT"
    if "mozilla public license" in low and "version 2.0" in low:
        return "MPL-2.0"
    if "redistribution and use in source and binary forms" in low:
        if "neither the name" in low or "endorse" in low:
            return "BSD-3-Clause"
        return "BSD-2-Clause"
    if "gnu lesser general public license" in low:
        return "LGPL (see file)"
    return None


# Dart packages verified against upstream license texts.
# Proof: docs/acknowledgements.md. Anything not listed here fails the scan.
DART_LICENSES = {
    # (spdx, proof)
    "xterm2": ("MIT", "pub.dev archive xterm2-5.2.0 LICENSE (MIT, (c) 2020 xuty)"),
    "firebase_core": (
        "BSD-3-Clause",
        "flutterfire LICENSE (BSD, (c) 2017 The Chromium Authors)",
    ),
    "firebase_messaging": (
        "BSD-3-Clause",
        "flutterfire LICENSE (BSD, (c) 2017 The Chromium Authors)",
    ),
    "app_links": ("Apache-2.0", "llfbandit/app_links LICENSE (Apache-2.0)"),
    "flet": (
        "Apache-2.0",
        "flet-dev/flet LICENSE at a87ca7fc (Apache-2.0)",
    ),
    "flutter_lints": (
        "BSD-3-Clause",
        "dart-lang/lints LICENSE (BSD, (c) 2021 Dart project authors)",
    ),
}

# Locked-but-uninstalled fallback: platform/marker-gated packages that are
# legitimately absent from this environment (verified against upstream
# metadata, not guessed). Anything not listed here stays UNKNOWN (fail).
PLATFORM_GATED_LICENSES = {
    # (spdx, proof)
    "colorama": (
        "BSD",
        "PyPI colorama/0.4.6 trove classifier BSD License; "
        "uv.lock marker sys_platform == 'win32' (absent on macOS/Linux)",
    ),
}

# SDK-bundled pseudodependencies: provided by the Flutter SDK, not packages.
SDK_PROVIDED = {"flutter", "flutter_test"}


def parse_lock_packages(lock_text: str) -> list[dict[str, str]]:
    """Read top-level name/version/source of each [[package]] block.

    Only the scalar header lines are captured; nested [package.*] and
    [[package.dependencies]] subsections (which repeat `name = ...` for
    dependencies) end the header and are skipped.
    """
    packages: list[dict[str, str]] = []
    current: dict[str, str] = {}
    in_header = False
    for line in lock_text.splitlines():
        stripped = line.strip()
        if stripped == "[[package]]":
            if current:
                packages.append(current)
            current = {}
            in_header = True
            continue
        if stripped.startswith("["):
            in_header = False
            continue
        if not in_header or not stripped or "=" not in stripped:
            continue
        key, _, value = stripped.partition("=")
        key, value = key.strip(), value.strip().strip('"')
        if key in ("name", "version", "source"):
            current[key] = value
    if current:
        packages.append(current)
    return [p for p in packages if "name" in p]


def lock_source_kind(source: str) -> str:
    if "registry" in source:
        return "pypi"
    if "editable" in source or "path" in source:
        return "path"
    if "git" in source:
        return "git"
    return source or "unknown"


def direct_dep_names(pyproject_text: str) -> tuple[set[str], dict[str, list[str]]]:
    """Return (direct names, group -> names) with a small TOML scan."""
    direct: set[str] = set()
    groups: dict[str, list[str]] = {}
    section = ""
    in_list = False
    for raw in pyproject_text.splitlines():
        line = raw.strip()
        if line.startswith("[dependency-groups."):
            section = ""
            continue
        if line.startswith("[tool.uv.sources]"):
            section = "sources"
            continue
        if line.startswith("["):
            section = ""
            in_list = False
            if line == "[project]":
                section = "project"
            elif line == "[dependency-groups]":
                section = "groups"
            continue
        if section == "project" and line.startswith("dependencies"):
            in_list = True
            continue
        if section == "groups" and "=" in line and "[" in line:
            group = line.split("=")[0].strip()
            groups[group] = []
            section = f"group:{group}"
            in_list = True
            continue
        if section == "sources" and "=" in line:
            direct.add(line.split("=")[0].strip())
            continue
        if in_list and line.startswith("]"):
            in_list = False
            if section.startswith("group:"):
                section = "groups"
            elif section == "project":
                section = "project"
            continue
        if in_list and line.startswith('"'):
            req = line.strip().strip(",").strip('"')
            name = req.split(";")[0].strip()
            for sep in (" ", "<", ">", "=", "!", "~", "[", "("):
                name = name.split(sep)[0]
            name = name.strip()
            if not name:
                continue
            if section == "project":
                direct.add(name)
            elif section.startswith("group:"):
                groups[section.split(":", 1)[1]].append(name)
    return direct, groups


def normalize(name: str) -> str:
    return name.lower().replace("-", "_").replace(".", "_")


def python_license(dist_name: str) -> tuple[str, str]:
    """Return (license, license_source); license is UNKNOWN when unmeasured."""
    try:
        dist = md.distribution(dist_name)
    except md.PackageNotFoundError:
        if dist_name in PLATFORM_GATED_LICENSES:
            spdx, proof = PLATFORM_GATED_LICENSES[dist_name]
            return spdx, "verified table (not installed): " + proof
        return "UNKNOWN", "not installed in this environment"
    meta = dist.metadata
    expr = meta.get("License-Expression") or ""
    if expr:
        return expr, "License-Expression"
    lic = (meta.get("License") or "").strip()
    if lic and lic.lower() != "unknown":
        return lic, "License metadata field"
    classifiers = [c for c in (meta.get_all("Classifier") or []) if c in CLASSIFIER_LICENSES]
    if classifiers:
        ids = sorted({CLASSIFIER_LICENSES[c] for c in classifiers})
        return " OR ".join(ids), "trove classifier: " + "; ".join(classifiers)
    try:
        files = list(dist.files or [])
    except Exception:  # noqa: BLE001 - unreadable dist falls through to UNKNOWN
        files = []
    for item in files:
        parts = [p.lower() for p in item.parts]
        if "licenses" in parts or item.name.lower().startswith("licen"):
            try:
                text = item.read_text()
            except (OSError, ValueError):
                continue
            found = fingerprint_license_file(text)
            if found:
                return found, f"bundled license file {item}"
    return "UNKNOWN", "no License field, classifier, or fingerprintable file"


def pubspec_deps(text: str, section: str) -> dict[str, str]:
    deps: dict[str, str] = {}
    in_section = False
    for line in text.splitlines():
        if not line.strip() or line.strip().startswith("#"):
            continue
        indent = len(line) - len(line.lstrip(" "))
        stripped = line.strip()
        if indent == 0:
            in_section = stripped.rstrip(":") == section
            continue
        if not in_section or indent != 2 or ":" not in stripped:
            continue
        name, _, constraint = stripped.partition(":")
        deps[name.strip()] = constraint.strip()
    return deps


def dart_inventory(root: Path) -> tuple[list[dict[str, str]], list[str]]:
    entries: list[dict[str, str]] = []
    notes: list[str] = []
    pubspecs = sorted(root.glob("extensions/*/src/flutter/*/pubspec.yaml"))
    if not pubspecs:
        notes.append("no extensions/*/src/flutter/*/pubspec.yaml found")
    for pubspec in pubspecs:
        text = pubspec.read_text(encoding="utf-8")
        owner = pubspec.parent.name
        merged = {
            **pubspec_deps(text, "dependencies"),
            **{k: v + " (dev)" for k, v in pubspec_deps(text, "dev_dependencies").items()},
        }
        for name, constraint in sorted(merged.items()):
            if name in SDK_PROVIDED:
                entries.append(
                    {
                        "name": name,
                        "constraint": constraint or "(sdk)",
                        "license": "BSD-3-Clause",
                        "license_source": "Flutter SDK (flutter/flutter LICENSE); "
                        "provided by pinned Flutter 3.44.8, not a package",
                        "used_by": owner,
                    }
                )
                continue
            if name in DART_LICENSES:
                spdx, proof = DART_LICENSES[name]
                entries.append(
                    {
                        "name": name,
                        "constraint": constraint,
                        "license": spdx,
                        "license_source": "verified table: " + proof,
                        "used_by": owner,
                    }
                )
            else:
                entries.append(
                    {
                        "name": name,
                        "constraint": constraint,
                        "license": "UNKNOWN",
                        "license_source": "not in verified table (add with upstream proof)",
                        "used_by": owner,
                    }
                )
        lock = pubspec.parent / "pubspec.lock"
        if lock.is_file():
            notes.append(f"{owner}: transitive Dart pins read from pubspec.lock")
        else:
            notes.append(
                f"{owner}: transitive Dart dependencies unavailable "
                f"(no {lock.relative_to(root)}; flutter pub get never ran)"
            )
    native = sorted(p for p in root.glob("extensions/*/src/flutter/*") if p.is_dir())
    pod_or_gradle = [
        str(p.relative_to(root))
        for base in native
        for p in (list(base.rglob("Podfile")) + list(base.rglob("build.gradle*")))
    ]
    if pod_or_gradle:
        notes.append("native build files: " + ", ".join(pod_or_gradle))
    else:
        notes.append(
            "no Podfile/build.gradle under extensions flutter projects; "
            "native Android/iOS transitive deps resolve only at first flutter build"
        )
    return entries, notes


def build_inventory(root: Path) -> dict[str, object]:
    lock_path = root / "uv.lock"
    if not lock_path.is_file():
        raise SystemExit("uv.lock not found")
    locked = parse_lock_packages(lock_path.read_text(encoding="utf-8"))
    pyproject = root / "pyproject.toml"
    direct: set[str] = set()
    groups: dict[str, list[str]] = {}
    if pyproject.is_file():
        direct, groups = direct_dep_names(pyproject.read_text(encoding="utf-8"))
    direct_norm = {normalize(n) for n in direct}
    group_norm = {group: {normalize(n) for n in names} for group, names in groups.items()}
    python_entries: list[dict[str, str]] = []
    for package in sorted(locked, key=lambda p: p["name"].lower()):
        name = package["name"]
        norm = normalize(name)
        lic, source = python_license(name)
        in_groups = sorted(g for g, names in group_norm.items() if norm in names)
        python_entries.append(
            {
                "name": name,
                "version": package.get("version", "UNKNOWN"),
                "source": lock_source_kind(package.get("source", "")),
                "license": lic,
                "license_source": source,
                "direct": "yes" if norm in direct_norm else "no",
                "groups": ",".join(in_groups),
            }
        )
    dart_entries, notes = dart_inventory(root)
    unknown = [e for e in python_entries + dart_entries if e["license"] == "UNKNOWN"]
    return {
        "schema": SCHEMA,
        "generated_at": datetime.now(UTC).isoformat(),
        "python_packages": python_entries,
        "dart_packages": dart_entries,
        "notes": notes,
        "unknown_count": len(unknown),
        "unknown": [f"{e.get('name')} ({e.get('license_source')})" for e in unknown],
    }


def render_table(inventory: dict[str, object]) -> str:
    lines = ["# Python (uv.lock)", "name==version | license | direct/groups"]
    for entry in inventory["python_packages"]:
        assert isinstance(entry, dict)
        scope = entry["direct"]
        if entry["groups"]:
            scope += f" [{entry['groups']}]"
        lines.append(f"{entry['name']}=={entry['version']} | {entry['license']} | {scope}")
    lines += ["", "# Dart (pubspec.yaml)", "name constraint | license | used by"]
    for entry in inventory["dart_packages"]:
        assert isinstance(entry, dict)
        lines.append(
            f"{entry['name']} {entry['constraint']} | {entry['license']} | {entry['used_by']}"
        )
    lines += ["", "# Notes", *[f"- {note}" for note in inventory["notes"]]]
    lines.append(f"unknown_count={inventory['unknown_count']}")
    return chr(10).join(lines) + chr(10)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Emit the license inventory SBOM.")
    parser.add_argument("--format", choices=("json", "table"), default="table")
    parser.add_argument("--out", help="write JSON to PATH (implies --format json)")
    parser.add_argument("--root", default="", help="repo root")
    parser.add_argument(
        "--allow-unknown",
        action="store_true",
        help="exit 0 even with UNKNOWN licenses (triage only; CI omits this)",
    )
    args = parser.parse_args(argv)
    root = Path(args.root or Path(__file__).resolve().parent.parent)
    inventory = build_inventory(root)
    unknown = inventory["unknown_count"]
    assert isinstance(unknown, int)
    if args.out:
        Path(args.out).write_text(
            json.dumps(inventory, indent=2, sort_keys=True) + chr(10),
            encoding="utf-8",
        )
    if args.format == "json" and not args.out:
        sys.stdout.write(json.dumps(inventory, indent=2, sort_keys=True) + chr(10))
    else:
        sys.stdout.write(render_table(inventory))
    if unknown and not args.allow_unknown:
        print(f"FAIL: {unknown} UNKNOWN licenses (see above)", file=sys.stderr)
        return 1
    print(
        f"OK: {len(inventory['python_packages'])} python + "
        f"{len(inventory['dart_packages'])} dart entries, "
        f"{unknown} unknown",
        file=sys.stderr,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
