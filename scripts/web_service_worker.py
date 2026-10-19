"""The pinned-shell service worker for the built web client.

Run by scripts/build_web_client.sh after every other post-processing step (fonts, icons, index
edits), so the BUILD digest covers the final shell files. Idempotent: running it again on an
already-patched output produces the same result.

    python scripts/web_service_worker.py OUT

Changes to OUT:
- flutter_bootstrap.js: `serviceWorkerSettings` is removed, so Flutter's loader registers nothing.
- index.html: one registration snippet and one keyboard focus guard (before </body>) and one
  `rd-build` meta (in <head>).
- flutter_service_worker.js: our worker replaces Flet's 784-byte stub at the same URL, with BUILD
  and the explicit shell list embedded.
- rd-sw-reset.html: the network-only kill switch.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
from pathlib import Path

HERE = Path(__file__).resolve().parent
DEFAULT_WORKER = HERE / "web" / "flutter_service_worker.js"
DEFAULT_RESET = HERE / "web" / "rd-sw-reset.html"

# Explicit shell list (no broad assets/** glob). Fallback Noto fonts are cached at runtime.
SHELL_FIXED = (
    "flutter_bootstrap.js",
    "flutter.js",
    "main.dart.js",
    "python.js",
    "manifest.json",
    "favicon.png",
    "favicon.ico",
    "assets/FontManifest.json",
    "assets/AssetManifest.bin.json",
    "assets/AssetManifest.bin",
    "assets/fonts/MaterialIcons-Regular.otf",
)
SHELL_GLOBS = ("icons/*", "splash/img/*")
# CanvasKit variants: NOT precached (each browser stores only the one it loads). They are cached
# at runtime on first fetch, cache-first, into the same build cache. They still count toward the
# BUILD digest, so a changed CanvasKit file never shares a cache name with the old one.
CANVASKIT_FILES = (
    "canvaskit/canvaskit.js",
    "canvaskit/canvaskit.wasm",
    "canvaskit/chromium/canvaskit.js",
    "canvaskit/chromium/canvaskit.wasm",
)
PAGE = "index.html"

SNIPPET_ID = "rd-sw-register"
CLIENT_SCRIPT = HERE / "web" / "rd-sw-client.js"
KEYBOARD_ID = "rd-keyboard-focus"
KEYBOARD_SCRIPT = HERE / "web" / "rd-keyboard-focus.js"
PAGE_SCRIPTS = ((SNIPPET_ID, CLIENT_SCRIPT), (KEYBOARD_ID, KEYBOARD_SCRIPT))


def snippet(script_id: str = SNIPPET_ID, path: Path = CLIENT_SCRIPT) -> str:
    """One page script, inlined verbatim into the served index."""
    body = path.read_text().rstrip("\n")
    if "</script" in body.lower():
        raise ValueError("the page script must not contain a closing script tag")
    return f'  <script id="{script_id}">\n{body}\n  </script>\n'


SNIPPET_RE = re.compile(
    r'[ \t]*<script id="(?:rd-sw-register|rd-keyboard-focus)">.*?</script>\n', re.S
)
META_RE = re.compile(r'[ \t]*<meta name="rd-build" content="[^"]*">\n')
CONFIG_MARKER = "serviceWorkerSettings: {"
SERVICE_SETTINGS_RE = re.compile(r"\n[ \t]*serviceWorkerSettings: \{.*?\n[ \t]*\},", re.S)


def patch_bootstrap(text: str) -> str:
    """Drop Flet's serviceWorkerSettings so Flutter's loader never registers a worker."""
    # The loader's own minified parameter (`serviceWorkerSettings:e`) stays; only Flet's config
    # object (`serviceWorkerSettings: {`) is removed, which is what makes the loader skip it.
    if CONFIG_MARKER not in text:
        return text
    patched, count = SERVICE_SETTINGS_RE.subn("", text, count=1)
    if count != 1 or CONFIG_MARKER in patched:
        raise ValueError("serviceWorkerSettings config block not recognised; not patching")
    return patched


def strip_meta(html: str) -> str:
    return META_RE.sub("", html)


def with_snippet(html: str) -> str:
    """Exactly one of each page script, before </body>."""
    html = SNIPPET_RE.sub("", html)
    if html.count("</body>") != 1:
        raise ValueError("index.html must contain exactly one </body>")
    scripts = "".join(snippet(script_id, path) for script_id, path in PAGE_SCRIPTS)
    return html.replace("</body>", scripts + "</body>", 1)


def with_meta(html: str, build: str) -> str:
    html = strip_meta(html)
    if html.count("<head>") != 1:
        raise ValueError("index.html must contain exactly one <head>")
    return html.replace("<head>", f'<head>\n  <meta name="rd-build" content="{build}">', 1)


def shell_files(out: Path) -> list[str]:
    """The precache list: explicit files that must exist, plus icons and splash globs."""
    missing = [name for name in SHELL_FIXED if not (out / name).is_file()]
    if missing:
        raise FileNotFoundError(f"shell files missing from the build: {missing}")
    found = set(SHELL_FIXED)
    for pattern in SHELL_GLOBS:
        for path in (out / pattern.split("/")[0]).glob(pattern.split("/", 1)[1]):
            if path.is_file():
                found.add(path.relative_to(out).as_posix())
    return sorted(found)


def digest_names(out: Path) -> list[str]:
    """Everything the BUILD id covers: the served page, the precache list, the runtime CanvasKit."""
    missing = [name for name in CANVASKIT_FILES if not (out / name).is_file()]
    if missing:
        raise FileNotFoundError(f"CanvasKit files missing from the build: {missing}")
    return sorted([PAGE, *shell_files(out), *CANVASKIT_FILES])


def runtime_hashes(out: Path) -> dict[str, str]:
    """SHA-256 per runtime-cacheable file (CanvasKit variants and every font). The worker stores a
    runtime fetch only when its bytes match this map."""
    missing = [name for name in CANVASKIT_FILES if not (out / name).is_file()]
    if missing:
        raise FileNotFoundError(f"CanvasKit files missing from the build: {missing}")
    names = set(CANVASKIT_FILES)
    fonts = out / "assets" / "fonts"
    if fonts.is_dir():
        names.update(
            path.relative_to(out).as_posix() for path in fonts.rglob("*") if path.is_file()
        )
    return {
        "/" + name: hashlib.sha256((out / name).read_bytes()).hexdigest() for name in sorted(names)
    }


def digest(out: Path, names: list[str]) -> str:
    """BUILD id: sha256 over the listed names and contents, and over the runtime hash map (so a
    changed font or CanvasKit file never shares a cache name). index.html is hashed without its
    rd-build meta, because that meta is derived from this digest."""
    outer = hashlib.sha256()
    for name in names:
        data = (out / name).read_bytes()
        if name == PAGE:
            data = strip_meta(data.decode()).encode()
        outer.update(name.encode() + b"\0" + hashlib.sha256(data).hexdigest().encode() + b"\n")
    runtime = json.dumps(runtime_hashes(out), sort_keys=True).encode()
    outer.update(b"runtime\0" + runtime + b"\n")
    return outer.hexdigest()[:16]


def render_worker(template: str, build: str, shell: list[str], runtime: dict[str, str]) -> str:
    return (
        template.replace("__BUILD__", build)
        .replace("__SHELL__", json.dumps(shell))
        .replace("__RUNTIME_HASHES__", json.dumps(runtime, sort_keys=True))
    )


def apply(
    out: Path, worker_template: Path = DEFAULT_WORKER, reset_template: Path = DEFAULT_RESET
) -> str:
    bootstrap = out / "flutter_bootstrap.js"
    bootstrap.write_text(patch_bootstrap(bootstrap.read_text()))
    page = out / PAGE
    html = with_snippet(strip_meta(page.read_text()))
    page.write_text(html)
    shell = shell_files(out)
    build = digest(out, digest_names(out))
    page.write_text(with_meta(html, build))
    (out / "flutter_service_worker.js").write_text(
        render_worker(worker_template.read_text(), build, shell, runtime_hashes(out))
    )
    (out / "rd-sw-reset.html").write_text(reset_template.read_text())
    return build


def verify(out: Path) -> str:
    """Recompute the digest from the output and check it is the one embedded everywhere."""
    build = digest(out, digest_names(out))
    page = (out / PAGE).read_text()
    if f'<meta name="rd-build" content="{build}">' not in page:
        raise ValueError("index.html rd-build meta does not match the digest")
    worker = (out / "flutter_service_worker.js").read_text()
    if f"const BUILD = '{build}';" not in worker:
        raise ValueError("worker BUILD does not match the digest")
    return build


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("out", type=Path)
    parser.add_argument("--worker-template", type=Path, default=DEFAULT_WORKER)
    parser.add_argument("--reset-template", type=Path, default=DEFAULT_RESET)
    parser.add_argument("--verify", action="store_true")
    args = parser.parse_args()
    if args.verify:
        print(verify(args.out))
        return
    print(apply(args.out, args.worker_template, args.reset_template))


if __name__ == "__main__":
    main()
