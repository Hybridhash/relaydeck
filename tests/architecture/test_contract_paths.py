"""Every `/v1` path lives in `contracts/paths.py`: the registered routes match its constants, no
other module spells a path, the embedded JavaScript only uses known paths, and builders keep the
URLs the call sites always produced."""

from __future__ import annotations

import ast
import re
from pathlib import Path
from typing import Any

from fastapi import FastAPI
from fastapi.routing import APIRoute
from starlette.routing import WebSocketRoute

from relaydeck.client.features.auth import web_recovery
from relaydeck.client.serve import WebSessionStore, build_web_app
from relaydeck.contracts import paths
from relaydeck.gateway.app import GatewayState, create_app
from relaydeck.gateway.backend import FakeBackend
from relaydeck.gateway.webpush_routes import WebPushSettings, build_router

SRC = Path(__file__).resolve().parents[2] / "src" / "relaydeck"
CONSTANTS = {
    value
    for name, value in vars(paths).items()
    if isinstance(value, str) and value.startswith("/v1") and not name.endswith("_PATTERN")
}
# A path inside a string: not the tail of a schema name or a host URL.
PATH_IN_STRING = re.compile(r"(?<![\w.])/v1(?!\w)")
ALLOWED_PREFIX_TEST = ("companion/runtime.py", "/v1/")
JS_MARKERS = ("fetch(", "location.pathname")


def _v1_paths(app: FastAPI) -> set[str]:
    return {
        route.path
        for route in app.routes
        if isinstance(route, APIRoute | WebSocketRoute) and route.path.startswith("/v1")
    }


def _registered_paths() -> set[str]:
    state = GatewayState(
        backend=FakeBackend(now_ms=lambda: 1_000_000),
        cursor_secret=b"x" * 32,
        now_ms=lambda: 1_000_000,
    )
    gateway = create_app(state)
    web, _ = build_web_app(
        gateway_state=state,
        gateway_app=gateway,
        flet_main=lambda page: None,
        public_origin="https://relay.example",
        cookie_secret=b"s" * 32,
        store=WebSessionStore("https://relay.example", b"s" * 32),
        dev_login=True,
    )
    push = FastAPI()
    push.include_router(
        build_router(
            store=None,  # type: ignore[arg-type]
            identity=lambda request: None,
            origin="https://relay.example",
            vapid_key=None,
            topic_key=b"t" * 32,
            settings=WebPushSettings(enabled=False, lock_screen_detail=False, subject=""),
            preferences=None,
        )
    )
    return _v1_paths(gateway) | _v1_paths(web) | _v1_paths(push)


def test_registered_v1_routes_equal_the_path_constants() -> None:
    registered = _registered_paths()
    assert registered - CONSTANTS == set(), "registered without a constant"
    assert CONSTANTS - registered == set(), "constant without a route"


def _docstring_nodes(tree: ast.AST) -> set[int]:
    found: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Module | ast.ClassDef | ast.FunctionDef | ast.AsyncFunctionDef):
            first = node.body[0] if node.body else None
            if (
                isinstance(first, ast.Expr)
                and isinstance(first.value, ast.Constant)
                and isinstance(first.value.value, str)
            ):
                found.add(id(first.value))
    return found


def test_no_module_but_paths_spells_a_v1_path() -> None:
    offenders: list[str] = []
    for file in sorted(SRC.rglob("*.py")):
        relative = file.relative_to(SRC).as_posix()
        if relative == "contracts/paths.py":
            continue
        tree = ast.parse(file.read_text())
        docstrings = _docstring_nodes(tree)
        for node in ast.walk(tree):
            if not (isinstance(node, ast.Constant) and isinstance(node.value, str)):
                continue
            value = node.value
            if id(node) in docstrings or not PATH_IN_STRING.search(value):
                continue
            if (relative, value) == ALLOWED_PREFIX_TEST:
                continue
            if any(marker in value for marker in JS_MARKERS):
                continue  # embedded JavaScript, checked by the next test
            offenders.append(f"{relative}:{node.lineno}: {value[:60]!r}")
    assert not offenders, offenders


def _js_strings() -> list[str]:
    strings = [web_recovery.RECOVERY_SCRIPT]
    tree = ast.parse((SRC / "client" / "serve.py").read_text())
    strings += [
        node.value
        for node in ast.walk(tree)
        if isinstance(node, ast.Constant)
        and isinstance(node.value, str)
        and any(marker in node.value for marker in JS_MARKERS)
    ]
    return strings


def test_embedded_javascript_only_uses_known_paths() -> None:
    literals: set[str] = set()
    for script in _js_strings():
        literals |= set(re.findall(r"""['"](/v1/[^'"?]*)['"]""", script))
    assert literals, "no /v1 literal found in the embedded scripts"
    assert literals - CONSTANTS == set()


def test_builders_produce_the_urls_the_call_sites_always_built() -> None:
    odd: Any = "a/b c:1"
    assert paths.terminal_socket("lease 1/x") == "/v1/terminal/lease%201%2Fx"
    assert paths.picture("abc") == "/v1/pictures/abc"
    assert paths.picture_content("abc") == "/v1/pictures/abc/content"
    assert paths.pairing_status("r1") == "/v1/pairing/r1/status"
    assert paths.pairing_redeem("r1") == "/v1/pairing/r1/redeem"
    assert paths.device(odd) == "/v1/devices/a%2Fb%20c%3A1"
    assert paths.enrollment(odd) == "/v1/enrollments/a%2Fb%20c%3A1"
    assert paths.enrollment_redeem("e1") == "/v1/enrollments/e1/redeem"
    assert paths.usage(odd) == "/v1/hosts/a%2Fb%20c%3A1/usage"
    assert paths.usage_limits_refresh("h1") == "/v1/hosts/h1/usage/limits/refresh"
    assert paths.host(odd) == "/v1/hosts/a%2Fb%20c%3A1"
    assert paths.host_snapshot("w1:p2") == "/v1/hosts/w1:p2/snapshot"
    assert paths.host_agents("h1") == "/v1/hosts/h1/agents"
    assert paths.agent_read("h 1", "a/1") == "/v1/hosts/h%201/agents/a%2F1/read"
    assert paths.host_commands("h1") == "/v1/hosts/h1/commands"
    assert paths.host_command("h 1", "c/1") == "/v1/hosts/h%201/commands/c%2F1"
    assert paths.terminal_attachments("h1", "w1:p2") == "/v1/hosts/h1/terminals/w1:p2/attachments"
    assert paths.terminal_control("h1", "t1") == "/v1/hosts/h1/terminals/t1/control"
    assert paths.attachment_leases("a1") == "/v1/terminal-attachments/a1/leases"
    assert paths.attachment("a1") == "/v1/terminal-attachments/a1"
    assert paths.lease("l1") == "/v1/terminal-leases/l1"
    assert paths.lease_renew("l1") == "/v1/terminal-leases/l1/renew"
