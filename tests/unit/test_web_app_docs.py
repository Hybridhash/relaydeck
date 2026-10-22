"""The apps serving the phone and browser (gateway, web app, mounted Flet app) expose FastAPI's
docs only while debugging."""

from __future__ import annotations

from pathlib import Path

import pytest

from relaydeck.client.serve import WebSessionStore, build_web_app
from relaydeck.gateway.app import create_app
from tests.support.companion.pairing_harness import Harness
from tests.support.pairing import ORIGIN

DOCS = {"/docs", "/docs/oauth2-redirect", "/redoc", "/openapi.json"}


@pytest.mark.parametrize("debug", [False, True])
def test_docs_routes_follow_the_debug_flag_on_both_apps(tmp_path: Path, debug: bool) -> None:
    h = Harness(tmp_path / "state")
    h.state.debug_docs = debug
    gateway = create_app(h.state)
    app, _ = build_web_app(
        gateway_state=h.state,
        gateway_app=gateway,
        flet_main=lambda page: None,
        public_origin=ORIGIN,
        cookie_secret=b"s" * 32,
        store=WebSessionStore(ORIGIN, b"s" * 32),
    )
    (flet_app,) = [r.app for r in app.routes if getattr(r, "path", None) == ""]
    for served in (gateway, app, flet_app):
        paths = {getattr(route, "path", "") for route in served.routes}
        assert (DOCS <= paths) is debug and not (DOCS & paths and not debug)
