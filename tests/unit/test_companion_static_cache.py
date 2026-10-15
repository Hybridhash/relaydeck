"""The companion revalidates every static web file; nothing is immutable.

All web URLs are unversioned (a Flutter upgrade changes canvaskit.js/wasm,
fonts and bundles at stable paths), so every non-API response carries
``no-cache`` and the browser revalidates with ETag/Last-Modified (304).
/v1/*, /ws and /healthz are untouched.
"""

from __future__ import annotations

import asyncio
import os
from pathlib import Path

import httpx
import pytest
from starlette.applications import Starlette
from starlette.responses import PlainTextResponse
from starlette.routing import Mount, Route
from starlette.staticfiles import StaticFiles

from relaydeck.companion.runtime import (
    NO_CACHE,
    cache_policy,
    static_cache,
    static_gzip,
)

BODY = "x" * 4096


def _app() -> Starlette:
    async def text(_request: object) -> PlainTextResponse:
        return PlainTextResponse(BODY)

    return Starlette(
        routes=[
            Route("/", text),
            Route("/main.dart.js", text),
            Route("/flutter_bootstrap.js", text),
            Route("/v1/snapshot.json", text),
            Route("/healthz", text),
        ]
    )


def _get(app: object, path: str, headers: dict[str, str] | None = None) -> httpx.Response:
    async def fetch() -> httpx.Response:
        transport = httpx.ASGITransport(app=app)  # type: ignore[arg-type]
        async with httpx.AsyncClient(transport=transport, base_url="http://t") as client:
            return await client.get(path, headers={"Accept-Encoding": "gzip", **(headers or {})})

    return asyncio.run(fetch())


@pytest.mark.parametrize(
    ("path", "want"),
    [
        ("/", NO_CACHE),
        ("/index.html", NO_CACHE),
        ("/main.dart.js", NO_CACHE),
        ("/flutter_bootstrap.js", NO_CACHE),
        ("/flutter_service_worker.js", NO_CACHE),
        ("/version.json", NO_CACHE),
        ("/manifest.json", NO_CACHE),
        ("/favicon.png", NO_CACHE),
        ("/canvaskit/canvaskit.wasm", NO_CACHE),
        ("/canvaskit/canvaskit.js", NO_CACHE),
        ("/assets/fonts/MaterialIcons-Regular.otf", NO_CACHE),
        ("/assets/FontManifest.json", NO_CACHE),
        ("/icons/Icon-192.png", NO_CACHE),
        ("/console", NO_CACHE),  # SPA shell served at route paths
        ("/v1/snapshot.json", None),
        ("/v1/events", None),
        ("/ws", None),
        ("/healthz", None),
    ],
)
def test_cache_policy_table(path: str, want: str | None) -> None:
    assert cache_policy(path) == want


def test_no_immutable_or_max_age_anywhere() -> None:
    paths = [
        "/",
        "/index.html",
        "/main.dart.js",
        "/canvaskit/canvaskit.wasm",
        "/assets/x.ttf",
        "/icons/Icon-512.png",
    ]
    for path in paths:
        policy = cache_policy(path)
        assert policy is not None
        assert "immutable" not in policy
        assert "max-age" not in policy


@pytest.mark.parametrize(
    ("path", "want"),
    [
        ("/", NO_CACHE),
        ("/main.dart.js", NO_CACHE),
        ("/flutter_bootstrap.js", NO_CACHE),
        ("/v1/snapshot.json", None),
        ("/healthz", None),
    ],
)
def test_middleware_stamps_header(path: str, want: str | None) -> None:
    response = _get(static_cache(_app()), path)
    assert response.status_code == 200
    assert response.headers.get("cache-control") == want
    assert response.text == BODY


def test_composes_with_gzip() -> None:
    response = _get(static_cache(static_gzip(_app())), "/main.dart.js")
    assert response.status_code == 200
    assert response.headers.get("content-encoding") == "gzip"
    assert response.headers.get("cache-control") == NO_CACHE
    assert response.text == BODY


def _static_app(directory: Path) -> StaticFiles:
    return StaticFiles(directory=directory, html=True)


def test_static_files_emit_etag_and_last_modified(tmp_path: Path) -> None:
    (tmp_path / "engine.js").write_bytes(b"console.log(1);" * 100)
    app = static_cache(static_gzip(_static_app(tmp_path)))
    response = _get(app, "/engine.js")
    assert response.status_code == 200
    assert response.headers.get("cache-control") == NO_CACHE
    etag = response.headers.get("etag")
    last_modified = response.headers.get("last-modified")
    assert etag, "StaticFiles must emit ETag"
    assert last_modified, "StaticFiles must emit Last-Modified"


def test_conditional_get_returns_304(tmp_path: Path) -> None:
    (tmp_path / "engine.js").write_bytes(b"console.log(1);" * 100)
    app = static_cache(static_gzip(_static_app(tmp_path)))
    first = _get(app, "/engine.js")
    etag = first.headers["etag"]
    second = _get(app, "/engine.js", {"If-None-Match": etag})
    assert second.status_code == 304
    assert second.headers.get("cache-control") == NO_CACHE
    assert second.headers.get("etag") == etag
    assert second.content == b""


def test_upgrade_same_path_new_bytes_return_200(tmp_path: Path) -> None:
    """Changed bytes at a stable path must reach the browser.

    Old ETag + new bytes => 200 with the new bytes, never a 304 from a
    year-long freshness stamp (there is none anymore).
    """
    target = tmp_path / "canvaskit.js"
    target.write_bytes(b"v1-engine" * 100)
    app = static_cache(static_gzip(_static_app(tmp_path)))
    first = _get(app, "/canvaskit.js")
    assert first.status_code == 200
    old_etag = first.headers["etag"]
    assert first.content == b"v1-engine" * 100

    target.write_bytes(b"v2-engine-longer" * 100)
    stat = os.stat(target)
    os.utime(target, (stat.st_atime, stat.st_mtime + 2))  # distinct ETag base

    upgraded = _get(app, "/canvaskit.js", {"If-None-Match": old_etag})
    assert upgraded.status_code == 200
    assert upgraded.content == b"v2-engine-longer" * 100
    assert upgraded.headers["etag"] != old_etag
    assert upgraded.headers.get("cache-control") == NO_CACHE


def test_mount_under_app_serves_index(tmp_path: Path) -> None:
    (tmp_path / "index.html").write_text("<title>RelayDeck</title>", encoding="utf-8")
    app = static_cache(
        Starlette(routes=[Mount("/", app=StaticFiles(directory=tmp_path, html=True))])
    )
    response = _get(app, "/")
    assert response.status_code == 200
    assert response.headers.get("cache-control") == NO_CACHE
    assert "RelayDeck" in response.text
    assert response.headers.get("etag")
