"""Native runtime boundary: hosted serve.py is deliberately not packaged.

Allowed repository layers: client, contracts, domain and content-free lease
diagnostics. External runtime packages: Flet, secure storage, both extensions,
httpx, websockets and cryptography (manifest runtime closure). Server packages
and developer extras are forbidden regardless of whether installed on the host.
"""

from __future__ import annotations

import ast
import importlib
import importlib.util
import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
SRC = ROOT / "src" / "relaydeck"
RUNTIME_LAYERS = (
    "relaydeck.client",
    "relaydeck.contracts",
    "relaydeck.domain",
    "relaydeck.lease_diagnostics",
    "relaydeck.version",
)
RUNTIME_EXTERNAL = frozenset(
    {
        "flet",
        "flet_secure_storage",
        "relaydeck_terminal",
        "relaydeck_notifications",
        "httpx",
        "websockets",
        "cryptography",
    }
)
HOST_ONLY = (
    "relaydeck.gateway",
    "relaydeck.connector",
    "relaydeck.companion",
    "relaydeck.client.serve",
    "fastapi",
    "uvicorn",
    "PIL",
    "qrcode",
    "flet_cli",
    "flet_web",
    "flet_desktop",
    "pytest",
    "pytest_asyncio",
    "numpy",
    "ruff",
    "mypy",
)


def _module_names(path: Path) -> set[str]:
    package = ".".join(path.relative_to(ROOT / "src").with_suffix("").parts)
    package = (
        package.removesuffix(".__init__")
        if path.name == "__init__.py"
        else package.rsplit(".", 1)[0]
    )
    imports: set[str] = set()
    for node in ast.walk(ast.parse(path.read_text())):
        if isinstance(node, ast.Import):
            imports.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            module = node.module or ""
            if node.level:
                module = importlib.util.resolve_name("." * node.level + module, package)
            imports.add(module)
            if module == "relaydeck":
                imports.update(f"relaydeck.{alias.name}" for alias in node.names)
        elif (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "import_module"
            and node.args
            and isinstance(node.args[0], ast.Constant)
            and isinstance(node.args[0].value, str)
        ):
            imports.add(node.args[0].value)
    return imports


def test_native_runtime_import_allowlist_has_hard_server_exclusion() -> None:
    failures = []
    for folder in (SRC / "client", SRC / "contracts", SRC / "domain"):
        for path in folder.rglob("*.py"):
            if path == SRC / "client" / "serve.py":
                continue
            for module in _module_names(path):
                if any(module == prefix or module.startswith(prefix + ".") for prefix in HOST_ONLY):
                    failures.append(f"{path.relative_to(ROOT)}: forbidden {module}")
                elif module.startswith("relaydeck."):
                    if not any(
                        module == prefix or module.startswith(prefix + ".")
                        for prefix in RUNTIME_LAYERS
                    ):
                        failures.append(f"{path.relative_to(ROOT)}: unknown layer {module}")
                elif module and module != "relaydeck":
                    top = module.split(".")[0]
                    if top not in sys.stdlib_module_names and top not in RUNTIME_EXTERNAL:
                        failures.append(f"{path.relative_to(ROOT)}: unknown dependency {module}")
    assert not failures, "\n".join(failures)


def test_extracted_contracts_have_only_stdlib_and_neutral_imports() -> None:
    paths = ("keys.py", "cursors.py", "pubkeys.py", "endpoints.py", "wire/terminal.py")
    for relative in paths:
        for module in _module_names(SRC / "contracts" / relative):
            assert (
                module.startswith("relaydeck.contracts")
                or module.split(".")[0] in sys.stdlib_module_names
            )


def test_packaged_client_imports_without_server_files_extras_or_editable_hooks(
    tmp_path: Path,
) -> None:
    app = tmp_path / "app"
    repo_package = app / "relaydeck"
    repo_package.mkdir(parents=True)
    shutil.copyfile(SRC / "__init__.py", repo_package / "__init__.py")
    shutil.copyfile(SRC / "lease_diagnostics.py", repo_package / "lease_diagnostics.py")
    shutil.copyfile(SRC / "version.py", repo_package / "version.py")
    for name in ("client", "contracts", "domain"):
        shutil.copytree(
            SRC / name,
            repo_package / name,
            ignore=shutil.ignore_patterns("serve.py", "__pycache__", ".DS_Store", ".flet"),
        )
    shutil.copyfile(SRC / "client/main.py", app / "main.py")
    modules = sorted(
        ".".join(path.relative_to(app).with_suffix("").parts).removesuffix(".__init__")
        for path in repo_package.rglob("*.py")
    )
    flet_file = importlib.import_module("flet").__file__
    assert flet_file is not None
    paths = [
        app,
        Path(flet_file).resolve().parent.parent,
        ROOT / "extensions/terminal/src",
        ROOT / "extensions/notifications/src",
    ]
    code = f"""
import importlib, importlib.abc, importlib.util, json, pathlib, runpy, sys
sys.dont_write_bytecode = True
runtime_paths = {[str(p) for p in paths]!r}
sys.path = runtime_paths + [p for p in sys.path if p and not p.endswith('site-packages')]
for name in ('relaydeck.gateway', 'relaydeck.connector', 'relaydeck.companion'):
    assert importlib.util.find_spec(name) is None, name
class DenyHostOnly(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if any(fullname == p or fullname.startswith(p + '.') for p in {HOST_ONLY!r}):
            raise AssertionError('native client attempted host-only import: ' + fullname)
sys.meta_path.insert(0, DenyHostOnly())
for name in {modules!r}:
    importlib.import_module(name)
runpy.run_path({str(app / "main.py")!r}, run_name='native_preflight')
for name, module in list(sys.modules.items()):
    if name.startswith('relaydeck.') and getattr(module, '__file__', None):
        assert pathlib.Path(module.__file__).resolve().is_relative_to({str(app)!r}), name
server_prefixes = ('relaydeck.gateway', 'relaydeck.connector', 'relaydeck.companion')
print(json.dumps({{'server_modules': [n for n in sys.modules if n.startswith(server_prefixes)]}}))
"""
    result = subprocess.run(
        [sys.executable, "-I", "-S", "-B", "-c", code],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)["server_modules"] == []


@pytest.mark.parametrize(
    ("old", "new", "names"),
    [
        (
            "relaydeck.gateway.terminal_wire",
            "relaydeck.contracts.wire.terminal",
            (
                "OutputChunk",
                "InputChunk",
                "encode_input",
                "decode_input",
                "encode_output",
                "decode_output",
                "encode_key",
                "encode_text",
                "encode_resize",
                "KIND_NAMES",
                "MAX_INPUT_CHUNK",
                "MAX_OUTPUT_CHUNK",
            ),
        ),
        (
            "relaydeck.connector.keys",
            "relaydeck.contracts.keys",
            (
                "normalize_combo",
                "is_valid_key",
                "MAX_KEY_LEN",
                "MODIFIERS",
                "NAMED_KEYS",
                "ALIASES",
            ),
        ),
        (
            "relaydeck.gateway.cursors",
            "relaydeck.contracts.cursors",
            ("MAX_CURSOR_LEN", "AFTER_SEQUENCE_HEADER"),
        ),
        ("relaydeck.gateway.policy", "relaydeck.contracts.endpoints", ("is_trusted_endpoint",)),
        (
            "relaydeck.connector.pubkey",
            "relaydeck.contracts.pubkeys",
            ("openssh_to_raw", "raw_to_openssh", "fingerprint_of_raw", "_ssh_blob"),
        ),
    ],
)
def test_old_paths_reexport_the_same_contract_objects(
    old: str, new: str, names: tuple[str, ...]
) -> None:
    legacy = importlib.import_module(old)
    neutral = importlib.import_module(new)
    for name in names:
        assert getattr(legacy, name) is getattr(neutral, name)


def test_client_cursor_contract_has_no_server_signing_api() -> None:
    from relaydeck.contracts import cursors
    from relaydeck.gateway.cursors import decode_cursor, encode_cursor

    assert not hasattr(cursors, "encode_cursor")
    assert not hasattr(cursors, "decode_cursor")
    payload = {"host_id": "home", "seq": 3}
    assert (
        decode_cursor(b"synthetic-secret", encode_cursor(b"synthetic-secret", payload)) == payload
    )


def test_legacy_endpoint_trust_is_distinct_from_strict_native_origin() -> None:
    from relaydeck.contracts.endpoints import is_trusted_endpoint, origin

    assert is_trusted_endpoint("http://trusted.example", frozenset({"trusted.example"}))
    assert not is_trusted_endpoint("http://trusted.example", frozenset())
    assert is_trusted_endpoint("ws://127.0.0.1", frozenset())
    assert is_trusted_endpoint("wss://synthetic.example", frozenset())
    assert not is_trusted_endpoint("file:///tmp/synthetic", frozenset())
    with pytest.raises(ValueError):
        origin("http://trusted.example")
    assert origin("https://synthetic.example/") == "https://synthetic.example"


def test_public_key_contract_preserves_open_ssh_roundtrip_and_fingerprint() -> None:
    from relaydeck.connector.pubkey import fingerprint_of_openssh
    from relaydeck.contracts.pubkeys import fingerprint_of_raw, openssh_to_raw, raw_to_openssh

    raw = bytes(range(32))
    line = raw_to_openssh(raw)
    assert openssh_to_raw(line) == raw
    assert fingerprint_of_openssh(line) == fingerprint_of_raw(raw)
    with pytest.raises(ValueError, match="not an ssh-ed25519 line"):
        openssh_to_raw("ssh-rsa synthetic")
    with pytest.raises(ValueError, match="bad openssh encoding"):
        openssh_to_raw("ssh-ed25519 !!!")
