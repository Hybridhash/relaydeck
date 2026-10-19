"""the pinned-shell service worker built by scripts/web_service_worker.py.

Static and build-level checks over a synthetic output tree (no Flutter build, no network). The
worker's runtime behaviour is checked by tests/ui/sw_worker_browser.cjs when node is available.
"""

from __future__ import annotations

import hashlib
import importlib.util
import json
import re
import shutil
import subprocess
from pathlib import Path
from types import ModuleType

import pytest

ROOT = Path(__file__).resolve().parents[2]
NODE = shutil.which("node") or str(Path.home() / ".local" / "bin" / "node")


def _module() -> ModuleType:
    spec = importlib.util.spec_from_file_location(
        "web_service_worker", ROOT / "scripts" / "web_service_worker.py"
    )
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


ws = _module()

BOOTSTRAP = """_flutter.loader.load({
    config: flutterConfig,
    serviceWorkerSettings: {
        serviceWorkerVersion: "3069144206" /* deprecated */,
    },
    onEntrypointLoaded: async function (engineInitializer) {}
});
var loader = {loadServiceWorker(e){if(!e||!("serviceWorker"in navigator))return Promise.resolve()}};
"""

INDEX = """<!DOCTYPE html>
<html>
<head>
  <meta charset="UTF-8">
  <title>RelayDeck</title>
</head>
<body>
  <script src="flutter_bootstrap.js" async></script>
</body>
</html>
"""


def _write(path: Path, data: str | bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if isinstance(data, bytes):
        path.write_bytes(data)
    else:
        path.write_text(data)


@pytest.fixture
def out(tmp_path: Path) -> Path:
    root = tmp_path / "out"
    for name in ws.SHELL_FIXED:
        _write(root / name, f"content of {name}")
    _write(root / "flutter_bootstrap.js", BOOTSTRAP)
    _write(root / "index.html", INDEX)
    _write(root / "flutter_service_worker.js", "/* Flet stub */")
    for name in ws.CANVASKIT_FILES:
        _write(root / name, f"canvaskit {name}")
    _write(root / "icons" / "Icon-192.png", b"png-192")
    _write(root / "splash" / "img" / "light-1x.png", b"splash")
    # Files that are never in the shell.
    _write(root / "assets" / "NOTICES", "licences")
    _write(root / "assets" / "app" / "app.zip", b"zip")
    _write(root / "pyodide" / "pyodide.mjs", "module")
    _write(root / "python-worker.js", "worker")
    _write(root / "assets" / "fonts" / "notosans" / "v1" / "a.woff2", b"font")
    return root


def test_apply_patches_bootstrap_adds_one_snippet_and_embeds_the_build(out: Path) -> None:
    build = ws.apply(out)
    bootstrap = (out / "flutter_bootstrap.js").read_text()
    assert "serviceWorkerSettings: {" not in bootstrap
    assert "serviceWorkerVersion" not in bootstrap
    assert bootstrap.count("loadServiceWorker(e)") == 1  # Flutter's loader code is left as is
    page = (out / "index.html").read_text()
    assert page.count('id="rd-sw-register"') == 1
    assert page.count('id="rd-keyboard-focus"') == 1
    assert "RelayDeck update notice" in page  # the page script is inlined verbatim
    assert "RelayDeck keyboard focus guard" in page
    register, guard = page.index('id="rd-sw-register"'), page.index('id="rd-keyboard-focus"')
    assert register < guard < page.index("</body>")
    assert page.count("register('/flutter_service_worker.js'") == 1
    assert "{ scope: '/', updateViaCache: 'none' }" in page
    assert "location.protocol === 'https:' || location.hostname === 'localhost'" in page
    assert page.count(f'<meta name="rd-build" content="{build}">') == 1
    worker = (out / "flutter_service_worker.js").read_text()
    assert f"const BUILD = '{build}';" in worker
    assert "__BUILD__" not in worker and "__SHELL__" not in worker


def test_build_id_is_the_digest_of_the_listed_files_and_verify_detects_tampering(out: Path) -> None:
    build = ws.apply(out)
    assert ws.verify(out) == build
    names = ws.digest_names(out)
    assert build == ws.digest(out, names)
    (out / "main.dart.js").write_text("changed")
    with pytest.raises(ValueError):
        ws.verify(out)
    # The digest is over the files, so it changes when a listed file changes.
    assert ws.digest(out, names) != build


def test_install_revalidates_with_no_cache_and_never_reloads(out: Path) -> None:
    ws.apply(out)
    worker = (out / "flutter_service_worker.js").read_text()
    install = worker[
        worker.index("async function precache") : worker.index("self.addEventListener('install'")
    ]
    assert install.count("cache: 'no-cache'") == 2  # the served page and the shell files
    assert "'reload'" not in worker


def test_apply_is_idempotent(out: Path) -> None:
    first = ws.apply(out)
    page_first = (out / "index.html").read_text()
    second = ws.apply(out)
    assert first == second
    assert (out / "index.html").read_text() == page_first
    assert (out / "index.html").read_text().count('id="rd-sw-register"') == 1
    assert (out / "index.html").read_text().count('id="rd-keyboard-focus"') == 1


def test_canvaskit_is_not_precached_but_is_in_the_digest(out: Path) -> None:
    shell = ws.shell_files(out)
    assert not any(name.startswith("canvaskit/") for name in shell)
    assert set(ws.CANVASKIT_FILES) <= set(ws.digest_names(out))
    before = ws.apply(out)
    (out / "canvaskit" / "chromium" / "canvaskit.wasm").write_bytes(b"new engine")
    assert ws.digest(out, ws.digest_names(out)) != before
    worker = (out / "flutter_service_worker.js").read_text()
    assert "'/canvaskit/'" in worker  # runtime cache-first, into this build's cache


def test_shell_list_is_explicit_and_excludes_everything_else(out: Path) -> None:
    shell = ws.shell_files(out)
    assert "assets/FontManifest.json" in shell and "assets/AssetManifest.bin" in shell
    assert "assets/fonts/MaterialIcons-Regular.otf" in shell
    assert "icons/Icon-192.png" in shell and "splash/img/light-1x.png" in shell
    forbidden = ("assets/NOTICES", "assets/app/app.zip", "pyodide/", "python-worker.js")
    for name in shell:
        assert not name.startswith("assets/fonts/notosans"), name  # fallback fonts: runtime only
        assert not any(name.startswith(prefix) or name == prefix for prefix in forbidden), name


def test_missing_shell_file_fails_the_build_loudly(out: Path) -> None:
    (out / "canvaskit" / "chromium" / "canvaskit.wasm").unlink()
    with pytest.raises(FileNotFoundError):
        ws.apply(out)


def test_bootstrap_without_the_config_block_is_left_alone(out: Path) -> None:
    text = "var loader = {loadServiceWorker(e){}};\n"
    assert ws.patch_bootstrap(text) == text


def test_worker_excludes_api_ws_non_get_cross_origin_pyodide_appzip_worker_and_reset(
    out: Path,
) -> None:
    ws.apply(out)
    worker = (out / "flutter_service_worker.js").read_text()
    body = worker[worker.index("function passesThrough") : worker.index("function contentTypeOk")]
    for rule in (
        "path === '/ws'",
        "path.startsWith('/v1/')",
        "path.startsWith('/pyodide/')",
        "path === '/python-worker.js'",
        "path.startsWith('/assets/app/')",
        "path === '/flutter_service_worker.js'",
        "path === '/rd-sw-reset.html'",
    ):
        assert rule in body, rule
    assert "request.method !== 'GET'" in worker
    assert "url.origin !== self.location.origin" in worker


def test_skip_waiting_only_for_a_single_window_activation_and_never_claims(out: Path) -> None:
    ws.apply(out)
    worker = (out / "flutter_service_worker.js").read_text()
    calls = [m.start() for m in re.finditer(r"skipWaiting\(", worker)]
    assert len(calls) == 1, "one skipWaiting call, behind the single-window check"
    body = worker[
        worker.index("async function activateIfOnlyWindow") : worker.index(
            "self.addEventListener('message'"
        )
    ]
    assert "skipWaiting(" in body and "onlyThisWindow" in body
    assert not re.search(r"clients\.claim\(", worker)


def test_activate_deletes_only_other_relaydeck_shell_caches(out: Path) -> None:
    ws.apply(out)
    worker = (out / "flutter_service_worker.js").read_text()
    activate = worker[worker.index("addEventListener('activate'") : worker.index("// Navigation:")]
    assert "key.startsWith(PREFIX) && key !== CACHE" in activate
    assert "caches.delete(key)" in activate


def test_cache_name_embeds_the_build_id(out: Path) -> None:
    build = ws.apply(out)
    worker = (out / "flutter_service_worker.js").read_text()
    assert "const CACHE = PREFIX + BUILD;" in worker
    assert "const PREFIX = 'relaydeck-shell-';" in worker
    assert f"relaydeck-shell-{build}" == "relaydeck-shell-" + build


def test_embedded_shell_list_matches_the_built_files(out: Path) -> None:
    build = ws.apply(out)
    worker = (out / "flutter_service_worker.js").read_text()
    match = re.search(r"const SHELL = (\[.*?\]);", worker)
    assert match
    assert json.loads(match.group(1)) == ws.shell_files(out)
    assert build in worker


def test_reset_page_is_static_network_only_and_clears_only_shell_caches(out: Path) -> None:
    ws.apply(out)
    reset = (out / "rd-sw-reset.html").read_text()
    assert not re.search(r"\.register\(", reset)
    assert "unregister()" in reset and "startsWith('relaydeck-shell-')" in reset
    assert "Offline cache cleared" in reset and 'href="/"' in reset
    assert "relaydeck-shell-" in reset


def _bodies(out: Path) -> Path:
    """The CanvasKit bodies as written by the fixture (the runtime hash map matches these)."""
    bodies = {f"/{name}": f"canvaskit {name}" for name in ws.CANVASKIT_FILES}
    path = out.parent / "bodies.json"
    path.write_text(json.dumps(bodies))
    return path


def test_runtime_behaviour_checks_pass_under_node(out: Path) -> None:
    if not Path(NODE).exists():
        pytest.skip("node is required for the worker behaviour check")
    ws.apply(out)
    result = subprocess.run(
        [
            NODE,
            str(ROOT / "tests" / "ui" / "sw_worker_browser.cjs"),
            str(out / "flutter_service_worker.js"),
            str(_bodies(out)),
        ],
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "ALL PASS" in result.stdout


def test_page_update_notice_passes_under_node(out: Path) -> None:
    if not Path(NODE).exists():
        pytest.skip("node is required for the page behaviour check")
    result = subprocess.run(
        [
            NODE,
            str(ROOT / "tests" / "ui" / "update_notice_page.cjs"),
            str(ROOT / "scripts" / "web" / "rd-sw-client.js"),
        ],
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "ALL PASS" in result.stdout


def test_page_keyboard_focus_guard_passes_under_node() -> None:
    if not Path(NODE).exists():
        pytest.skip("node is required for the page behaviour check")
    result = subprocess.run(
        [
            NODE,
            str(ROOT / "tests" / "ui" / "keyboard_focus_page.cjs"),
            str(ROOT / "scripts" / "web" / "rd-keyboard-focus.js"),
        ],
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "ALL PASS" in result.stdout


def test_runtime_hash_map_covers_canvaskit_and_fonts_and_the_digest_covers_it(out: Path) -> None:
    build = ws.apply(out)
    worker = (out / "flutter_service_worker.js").read_text()
    match = re.search(r"const RUNTIME_HASHES = (\{.*?\});", worker)
    assert match
    hashes = json.loads(match.group(1))
    assert set(f"/{name}" for name in ws.CANVASKIT_FILES) <= set(hashes)
    assert "/assets/fonts/notosans/v1/a.woff2" in hashes
    assert hashes["/assets/fonts/notosans/v1/a.woff2"] == hashlib.sha256(b"font").hexdigest()
    (out / "assets" / "fonts" / "notosans" / "v1" / "a.woff2").write_bytes(b"other font")
    assert ws.digest(out, ws.digest_names(out)) != build


def test_worker_and_reset_script_pass_node_check(out: Path) -> None:
    if not Path(NODE).exists():
        pytest.skip("node is required for the syntax check")
    ws.apply(out)
    subprocess.run([NODE, "--check", str(out / "flutter_service_worker.js")], check=True)
    reset = (out / "rd-sw-reset.html").read_text()
    script = re.search(r"<script>(.*?)</script>", reset, re.S)
    assert script
    tmp = out.parent / "reset-check.js"
    tmp.write_text(script.group(1))
    subprocess.run([NODE, "--check", str(tmp)], check=True)
